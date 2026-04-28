"""Chat endpoint."""
import re
import time
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import get_settings
from app.models.schemas import ChatRequest, ChatResponse, ToolCallTrace
from app.services.ai_router import AIRouter
from app.services.erpnext_client import ERPNextClient
from app.services.query_classifier import classify_query, run_shortcut

router = APIRouter()
settings = get_settings()


# ──────────────────────────────────────────────────────────────────────
# Provider cooldown — process-local memory of recent 429s.
#
# When a provider returns 429, we extract the "try again in Xm Ys" hint
# from the error body and skip that provider until the cooldown elapses.
# This stops us from burning ~10K input tokens per attempt against a
# provider we already know is rate-limited, which was the root cause of
# the cascading 503s ("all providers exhausted simultaneously").
#
# Process-local is fine: Cloud Run has min-instances=0 so cold starts
# refresh memory anyway, and a misfired retry across instances is cheap.
# ──────────────────────────────────────────────────────────────────────
_cooldown_until: dict[str, float] = {}
_DEFAULT_COOLDOWN_SECS = 60.0   # Conservative default if we can't parse the hint
_MAX_COOLDOWN_SECS = 90 * 60.0  # 90 min cap to avoid silly long blackouts


def _provider_in_cooldown(provider: str) -> float:
    """Return seconds remaining in cooldown, or 0 if available."""
    until = _cooldown_until.get(provider, 0.0)
    remaining = until - time.time()
    return max(0.0, remaining)


def _set_provider_cooldown(provider: str, error: Exception) -> float:
    """Parse error message for retry hint and set cooldown. Returns secs set."""
    msg = str(error)
    secs = _DEFAULT_COOLDOWN_SECS

    # Groq: "Please try again in 42m44.544s"
    m = re.search(r"try again in\s+(?:(\d+)m)?(\d+(?:\.\d+)?)s", msg, re.IGNORECASE)
    if m:
        mins = int(m.group(1) or 0)
        s = float(m.group(2) or 0)
        secs = mins * 60 + s
    else:
        # Anthropic: "rate limit of 10,000 input tokens per minute" → 60s baseline
        if "per minute" in msg.lower():
            secs = 60.0
        # Gemini RESOURCE_EXHAUSTED is daily; don't try again for an hour.
        elif "resource_exhausted" in msg.lower() or "quota" in msg.lower():
            secs = 60 * 60.0

    secs = max(15.0, min(secs, _MAX_COOLDOWN_SECS))
    _cooldown_until[provider] = time.time() + secs
    logger.info(f"[cooldown] {provider} skipped for {secs:.0f}s")
    return secs


def _is_rate_limit_error(e: Exception) -> bool:
    """Detect 429 / rate-limit exhaustion across all SDKs."""
    s = str(e).lower()
    return any(
        marker in s
        for marker in ["429", "rate limit", "quota", "exhausted", "resource_exhausted"]
    )


def _is_soft_provider_failure(e: Exception) -> bool:
    """Errors that should escalate to another provider rather than 500.

    Includes:
    - Rate limits (already handled separately)
    - Hallucinated tool calls (Groq Llama tends to do this)
    - Malformed JSON in tool arguments
    - Invalid request errors that aren't user-fault
    - Network connection errors (Cloud Run cold-start TLS handshake races,
      transient DNS hiccups, upstream provider 5xx). Better to fall back
      to Claude than to 500 the user.
    """
    # Type-based detection for httpx/openai/anthropic connection wrappers.
    type_name = type(e).__name__
    connection_types = {
        "APIConnectionError",        # openai SDK
        "APITimeoutError",           # openai SDK
        "APIConnectionError",        # anthropic SDK (same name)
        "ConnectError",              # httpx
        "ConnectTimeout",            # httpx
        "ReadTimeout",               # httpx
        "RemoteProtocolError",       # httpx
        "ServiceUnavailableError",
    }
    if type_name in connection_types:
        return True

    s = str(e).lower()
    return any(
        marker in s
        for marker in [
            "tool_use_failed",
            "tool call validation failed",
            "tool call failed",
            "function not found",
            "invalid_request_error",
            "json decode",
            "malformed",
            "unable to parse",
            "connection error",
            "connection reset",
            "connection refused",
            "connection aborted",
            "temporarily unavailable",
            "503 service unavailable",
            "502 bad gateway",
            "504 gateway timeout",
        ]
    )


_GAVE_UP_MARKERS = (
    # Arabic
    "لم أتمكن من إكمال الطلب",
    "عذراً، لم أتمكن",
    "وصل النظام إلى الحد",
    # Sorani
    "نەمتوانی",
    "نەتوانرا",
    # English
    "i couldn't complete",
    "i could not complete",
    "i'm unable to",
    "unable to complete the request",
)


def _looks_like_gave_up(resp: ChatResponse) -> bool:
    """The agent loop hit MAX_AGENT_ITERATIONS without producing a real answer.
    Treat these as soft failures so the next provider can try.
    """
    msg = (resp.message or "").strip()
    if not msg:
        return True
    return any(marker in msg for marker in _GAVE_UP_MARKERS)


async def _try_provider(provider: str, req, erp, user) -> ChatResponse:
    ai = AIRouter(provider=provider)
    resp = await ai.chat(
        messages=req.messages,
        erpnext_client=erp,
        user_full_name=user.full_name,
        user_roles=user.roles,
        lang=req.lang,
        user_memory=req.user_memory,
    )
    if _looks_like_gave_up(resp):
        # Don't return the canned apology — let the caller try a smarter model.
        # Strip the stale table_data too so we don't render leftover empty rows.
        raise _AgentGaveUp(provider, resp)
    resp.provider_used = provider
    return resp


class _AgentGaveUp(Exception):
    """Raised when an AI agent loop ran out of iterations without an answer."""
    def __init__(self, provider: str, resp: ChatResponse):
        super().__init__(f"{provider} agent loop exhausted")
        self.provider = provider
        self.resp = resp


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
):
    erp = ERPNextClient(
        base_url=user.erp_url,
        api_key=user.api_key,
        api_secret=user.api_secret,
    )

    # ────────────────────────────────────────────────────────────────────
    # Smart routing: classify the latest user message before paying for AI.
    # ────────────────────────────────────────────────────────────────────
    user_msgs = [m for m in req.messages if m.role == "user"]
    latest_query = user_msgs[-1].content if user_msgs else ""
    classification = await classify_query(latest_query)
    logger.info(
        f"classified[{classification.tier}]: '{latest_query[:60]}' "
        f"({classification.reason})"
    )

    # Tier 1: Deterministic shortcut — no AI involved at all.
    if classification.tier == "deterministic" and classification.shortcut:
        try:
            text = await run_shortcut(
                classification.shortcut,
                classification.args or {},
                erp,
                user.full_name,
            )
            return ChatResponse(
                message=text,
                tool_calls=[ToolCallTrace(
                    tool_name=f"shortcut:{classification.shortcut}",
                    arguments=classification.args or {},
                    result_summary="استجابة فورية بدون AI",
                )],
                provider_used="deterministic",
            )
        except Exception as e:
            logger.warning(f"Shortcut '{classification.shortcut}' failed, falling through to AI: {e}")
            # Fall through to AI

    # Tier 2 & 3: pick provider chain based on complexity.
    # Escalation rule: even if user picked a cheap model, complex queries
    # auto-escalate to Claude. The provider badge tells them what happened.
    user_choice = req.ai_provider
    is_complex = classification.tier == "complex"

    if is_complex and user_choice in ("groq", "gemini"):
        # Cheap models tend to spin in circles on multi-step ERP analysis.
        # Try the cheap model first to honor the user's choice, then escalate.
        primary = user_choice
        logger.info(
            f"complex query — primary={primary}, will escalate to claude on failure"
        )
    elif user_choice:
        primary = user_choice
    elif is_complex:
        primary = "claude"
    else:
        # Simple → fastest free option
        primary = "groq"

    # Fallback chain: when primary fails OR returns canned apology, try
    # other providers in order. Claude near the front for complex queries.
    fallback_order = [primary]
    if is_complex and primary != "claude":
        fallback_order.append("claude")
    for alt in ("claude", "groq", "gemini", "openai"):
        if alt not in fallback_order:
            fallback_order.append(alt)

    last_error: Exception | None = None
    last_gave_up_resp: ChatResponse | None = None
    for provider in fallback_order:
        # Skip providers we already know are rate-limited until cooldown expires.
        # Saves the 5-15K input tokens we'd otherwise burn on a guaranteed 429.
        cooldown_remaining = _provider_in_cooldown(provider)
        if cooldown_remaining > 0:
            logger.info(
                f"Provider {provider} in cooldown ({cooldown_remaining:.0f}s left), skipping"
            )
            continue
        try:
            return await _try_provider(provider, req, erp, user)
        except _AgentGaveUp as e:
            logger.warning(f"Provider {provider} gave up — escalating to next provider")
            last_gave_up_resp = e.resp
            last_error = e
            continue
        except ValueError as e:
            logger.info(f"Provider {provider} not available: {e}")
            last_error = e
            continue
        except Exception as e:
            if _is_rate_limit_error(e):
                _set_provider_cooldown(provider, e)
                logger.warning(
                    f"Provider {provider} rate-limit-classified. Falling back. "
                    f"type={type(e).__name__} msg={str(e)[:300]}"
                )
                last_error = e
                continue
            if _is_soft_provider_failure(e):
                logger.warning(
                    f"Provider {provider} soft-failed (hallucinated tool / bad request). "
                    f"Falling back. type={type(e).__name__} msg={str(e)[:300]}"
                )
                last_error = e
                continue
            logger.exception(f"Provider {provider} failed")
            raise HTTPException(status_code=500, detail=str(e))

    # All providers gave up. Return the LAST gave-up response so user sees
    # a polite Arabic message instead of an HTTP 503.
    if last_gave_up_resp is not None:
        last_gave_up_resp.provider_used = "exhausted"
        last_gave_up_resp.table_data = None  # don't leave stale table from exploration
        return last_gave_up_resp

    # All providers exhausted — usually because every free-tier daily quota
    # got hit at the same time. Surface a 200 with an Arabic explanation
    # and the soonest retry time, instead of a generic 503 dump.
    soonest_retry = None
    for p, until in _cooldown_until.items():
        remaining = until - time.time()
        if remaining > 0 and (soonest_retry is None or remaining < soonest_retry):
            soonest_retry = remaining
    if soonest_retry is not None:
        mins = int(soonest_retry // 60)
        secs = int(soonest_retry % 60)
        wait_text = f"{mins} دقيقة و{secs} ثانية" if mins else f"{secs} ثانية"
        msg = (
            "وصلنا الحد الأقصى للأسئلة المجانية على كل مزوّدي الذكاء الاصطناعي "
            f"(Groq, Claude, Gemini). يُرجى الانتظار حوالي {wait_text} ثم المحاولة. "
            "للاستخدام المكثّف، يمكن إيداع رصيد مدفوع على Anthropic أو OpenAI لرفع الحدود."
        )
        return ChatResponse(
            message=msg, tool_calls=[], table_data=None, provider_used="exhausted",
        )

    raise HTTPException(
        status_code=503,
        detail=f"كل مزودي الذكاء الاصطناعي غير متاحين حالياً. آخر خطأ: {last_error}",
    )
