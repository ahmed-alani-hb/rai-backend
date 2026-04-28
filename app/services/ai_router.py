"""AI router — supports OpenAI, Claude, and Gemini with one interface."""
import json
from datetime import date
from typing import Any, Literal, Optional

import httpx
from loguru import logger
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic

from app.core.config import get_settings
from app.models.schemas import ChatMessage, ChatResponse, ToolCallTrace
from app.prompts.system_ar import build_system_prompt
from app.prompts.system_ckb import build_system_prompt_ckb
from app.prompts.system_en import build_system_prompt_en
from app.services.business_context import format_for_prompt, get_business_context
from app.services.erpnext_client import ERPNextClient
from app.services.tools import (
    execute_tool,
    to_claude_tools,
    to_openai_tools,
    to_gemini_tools,
    to_groq_tools,
)

settings = get_settings()

# Cloud Run's first outbound TCP/TLS handshake to a new host can take
# several seconds on a cold instance. The OpenAI SDK's default timeout is
# tight enough that this races and surfaces as APIConnectionError. We
# give it explicit headroom and let the SDK retry idempotent failures.
_OPENAI_COMPAT_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=10.0)
_OPENAI_COMPAT_RETRIES = 2

# Tool calls whose results are always exploration dumps and shouldn't be
# rendered as the chat's bottom table. ``get_list`` in particular is often
# used by the agent as a last-mile data pull (500 rows of Sales Invoice)
# whose columns don't match a clean human view; surfacing it produced the
# "header-only / empty rows" UX bug. The agent's text answer is the source
# of truth — table_data is just a nicety for short, focused result sets.
_NON_DISPLAY_TOOLS = {"get_list", "get_general_ledger"}
_TABLE_MAX_ROWS = 50


def _maybe_use_as_table(
    current: Optional[list[dict[str, Any]]],
    tool_name: str,
    result: Any,
) -> Optional[list[dict[str, Any]]]:
    """Decide whether to display this tool result as the chat's table.

    Keeps the previous table if the new result isn't suitable so a useful
    earlier table isn't clobbered by a later exploration call.
    """
    if not isinstance(result, list) or not result:
        return current
    if tool_name in _NON_DISPLAY_TOOLS:
        return current
    if len(result) > _TABLE_MAX_ROWS:
        return current
    # Only displayable if rows are dicts with at least one populated field.
    sample = result[0]
    if not isinstance(sample, dict):
        return current
    if not any(v not in (None, "", [], {}) for v in sample.values()):
        return current
    return result

# Agent loop iteration caps. Smarter models get more attempts because they
# typically use them well; cheaper models tend to spin in circles, so we
# cut them off sooner to escalate to Claude faster.
MAX_AGENT_ITERATIONS = 5
MAX_AGENT_ITERATIONS_GROQ = 3
MAX_AGENT_ITERATIONS_GEMINI = 3


class AIRouter:
    def __init__(self, provider: Literal["openai", "claude", "gemini", "groq"] = "groq"):
        self.provider = provider
        if provider == "openai":
            if not settings.openai_api_key:
                raise ValueError("OPENAI_API_KEY غير معرّف")
            self.openai = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=_OPENAI_COMPAT_TIMEOUT,
                max_retries=_OPENAI_COMPAT_RETRIES,
            )
        elif provider == "claude":
            if not settings.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY غير معرّف")
            self.anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
        elif provider == "gemini":
            if not settings.gemini_api_key:
                raise ValueError(
                    "GEMINI_API_KEY غير معرّف. أضفه إلى .env واحصل على مفتاح "
                    "مجاني من https://aistudio.google.com/apikey"
                )
            try:
                from google import genai  # type: ignore
            except ImportError as e:
                raise ValueError(
                    "حزمة google-genai غير مثبّتة. شغّل: "
                    "pip install google-genai"
                ) from e
            self.genai = genai.Client(api_key=settings.gemini_api_key)
        elif provider == "groq":
            if not settings.groq_api_key:
                raise ValueError(
                    "GROQ_API_KEY غير معرّف. أضفه إلى .env واحصل على مفتاح "
                    "مجاني من https://console.groq.com/keys"
                )
            # Groq is OpenAI-compatible — reuse the OpenAI SDK pointed at
            # their endpoint. Tool calling, JSON mode, etc. all work identically.
            self.groq = AsyncOpenAI(
                api_key=settings.groq_api_key,
                base_url="https://api.groq.com/openai/v1",
                timeout=_OPENAI_COMPAT_TIMEOUT,
                max_retries=_OPENAI_COMPAT_RETRIES,
            )

    async def chat(
        self,
        messages: list[ChatMessage],
        erpnext_client: ERPNextClient,
        user_full_name: str = "",
        user_roles: Optional[list[str]] = None,
        lang: str = "ar",
        user_memory: Optional[list[str]] = None,
    ) -> ChatResponse:
        # Fetch business context (cached 1h) and inject into system prompt.
        # Wrapped wide because failures here are non-fatal — fall back to
        # an empty context block, which is better than 500-ing the chat.
        ctx_text = ""
        try:
            ctx = await get_business_context(erpnext_client)
            ctx_text = format_for_prompt(ctx)
        except Exception as e:
            logger.exception(f"business context fetch failed (non-fatal): {e}")

        # Append user's saved memory entries to the business-context block
        # so the LLM treats them as background facts. Headed in the user's
        # language so the AI doesn't get confused about the directive.
        memory_text = _format_user_memory(user_memory or [], lang)

        # Pick the system prompt variant matching the user's locale. Each
        # variant tells the LLM to reply in that language and ships its own
        # routing table; the tool definitions are still language-neutral.
        prompt_builder = {
            "en": build_system_prompt_en,
            "ckb": build_system_prompt_ckb,
        }.get(lang, build_system_prompt)

        system_prompt = prompt_builder(
            today=str(date.today()),
            user_full_name=user_full_name,
            roles=user_roles or [],
            business_context=(ctx_text + memory_text).strip(),
        )
        user_msgs = [m.model_dump(exclude_none=True) for m in messages]

        if self.provider == "openai":
            return await self._chat_openai(system_prompt, user_msgs, erpnext_client)
        elif self.provider == "claude":
            return await self._chat_claude(system_prompt, user_msgs, erpnext_client)
        elif self.provider == "groq":
            return await self._chat_groq(system_prompt, user_msgs, erpnext_client)
        else:
            return await self._chat_gemini(system_prompt, user_msgs, erpnext_client)

    # ---------------- OpenAI ----------------
    async def _chat_openai(self, system_prompt, user_msgs, erpnext_client):
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for m in user_msgs:
            msgs.append({"role": m["role"], "content": m["content"]})

        traces: list[ToolCallTrace] = []
        last_table: Optional[list[dict[str, Any]]] = None

        for _ in range(MAX_AGENT_ITERATIONS):
            resp = await self.openai.chat.completions.create(
                model=settings.openai_model,
                messages=msgs,
                tools=to_openai_tools(),
                temperature=0.2,
            )
            choice = resp.choices[0].message

            if choice.tool_calls:
                msgs.append({
                    "role": "assistant",
                    "content": choice.content,
                    "tool_calls": [tc.model_dump() for tc in choice.tool_calls],
                })
                for tc in choice.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments or "{}")
                    logger.info(f"[OpenAI] tool: {name}({args})")
                    result = await execute_tool(name, args, erpnext_client)
                    last_table = _maybe_use_as_table(last_table, name, result)
                    traces.append(ToolCallTrace(
                        tool_name=name, arguments=args, result_summary=_summarize(result),
                    ))
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    })
                continue

            return ChatResponse(message=choice.content or "", tool_calls=traces, table_data=last_table)

        return ChatResponse(
            message="عذراً، لم أتمكن من إكمال الطلب.",
            tool_calls=traces, table_data=last_table,
        )

    # ---------------- Groq (Llama 3.3) ----------------
    async def _chat_groq(self, system_prompt, user_msgs, erpnext_client):
        """Groq Llama 3.3 70B with OpenAI-compatible tool calling.

        Sub-second response, 14,400 free RPD. Quality is good for simple
        invoice/customer queries; Claude still better for deep reasoning.
        """
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for m in user_msgs:
            msgs.append({"role": m["role"], "content": m["content"]})

        traces: list[ToolCallTrace] = []
        last_table: Optional[list[dict[str, Any]]] = None
        # Track repeated tool calls — if Groq calls the same tool with same args
        # twice, it's spinning. Abort early and let Claude take over.
        seen_calls: set[str] = set()

        for _ in range(MAX_AGENT_ITERATIONS_GROQ):
            try:
                resp = await self.groq.chat.completions.create(
                    model=settings.groq_chat_model,
                    messages=msgs,
                    tools=to_groq_tools(),
                    temperature=0.2,
                )
            except Exception as e:
                # Surface the underlying httpx cause (DNS / TLS / timeout)
                # so Cloud Run logs tell us why the connection failed.
                cause = getattr(e, "__cause__", None)
                logger.warning(
                    f"[Groq] request failed: {type(e).__name__}: {str(e)[:300]} "
                    f"cause={type(cause).__name__ if cause else 'none'}: "
                    f"{str(cause)[:300] if cause else ''}"
                )
                raise
            choice = resp.choices[0].message

            if choice.tool_calls:
                msgs.append({
                    "role": "assistant",
                    "content": choice.content,
                    "tool_calls": [tc.model_dump() for tc in choice.tool_calls],
                })
                hit_repeat = False
                for tc in choice.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    call_signature = f"{name}:{json.dumps(args, sort_keys=True)}"
                    if call_signature in seen_calls:
                        logger.warning(f"[Groq] repeated call detected: {name}({args}) — aborting")
                        hit_repeat = True
                        break
                    seen_calls.add(call_signature)

                    logger.info(f"[Groq] tool: {name}({args})")
                    result = await execute_tool(name, args, erpnext_client)
                    last_table = _maybe_use_as_table(last_table, name, result)
                    traces.append(ToolCallTrace(
                        tool_name=name, arguments=args, result_summary=_summarize(result),
                    ))
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    })
                if hit_repeat:
                    break
                continue

            return ChatResponse(message=choice.content or "", tool_calls=traces, table_data=last_table)

        return ChatResponse(
            message="عذراً، لم أتمكن من إكمال الطلب.",
            tool_calls=traces, table_data=last_table,
        )

    # ---------------- Claude ----------------
    async def _chat_claude(self, system_prompt, user_msgs, erpnext_client):
        msgs: list[dict[str, Any]] = []
        for m in user_msgs:
            if m["role"] in ("user", "assistant"):
                msgs.append({"role": m["role"], "content": m["content"]})

        traces: list[ToolCallTrace] = []
        last_table: Optional[list[dict[str, Any]]] = None

        for _ in range(MAX_AGENT_ITERATIONS):
            resp = await self.anthropic.messages.create(
                model=settings.anthropic_model,
                max_tokens=2048,
                system=system_prompt,
                tools=to_claude_tools(),
                messages=msgs,
            )

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            text_blocks = [b for b in resp.content if b.type == "text"]

            if tool_uses:
                msgs.append({"role": "assistant", "content": resp.content})
                tool_results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    args = tu.input or {}
                    logger.info(f"[Claude] tool: {tu.name}({args})")
                    result = await execute_tool(tu.name, args, erpnext_client)
                    last_table = _maybe_use_as_table(last_table, tu.name, result)
                    traces.append(ToolCallTrace(
                        tool_name=tu.name, arguments=args, result_summary=_summarize(result),
                    ))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    })
                msgs.append({"role": "user", "content": tool_results})
                continue

            text = "\n".join(b.text for b in text_blocks)
            return ChatResponse(message=text, tool_calls=traces, table_data=last_table)

        return ChatResponse(
            message="عذراً، لم أتمكن من إكمال الطلب.",
            tool_calls=traces, table_data=last_table,
        )

    # ---------------- Gemini ----------------
    async def _chat_gemini(self, system_prompt, user_msgs, erpnext_client):
        """Gemini 2.5 Flash via google-genai SDK.

        Google's content shape is different: every turn is a Content with parts.
        Function calls are Part(function_call=...) and we reply with
        Part(function_response=...).
        """
        from google.genai import types as gtypes  # type: ignore

        # Convert chat history to Gemini Contents
        contents: list[Any] = []
        for m in user_msgs:
            role = "user" if m["role"] == "user" else "model"
            contents.append(
                gtypes.Content(
                    role=role,
                    parts=[gtypes.Part(text=m["content"])],
                )
            )

        traces: list[ToolCallTrace] = []
        last_table: Optional[list[dict[str, Any]]] = None

        config = gtypes.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=to_gemini_tools(),
            temperature=0.2,
        )

        for _ in range(MAX_AGENT_ITERATIONS):
            resp = await self.genai.aio.models.generate_content(
                model=settings.gemini_model,
                contents=contents,
                config=config,
            )

            candidate = resp.candidates[0] if resp.candidates else None
            if not candidate or not candidate.content or not candidate.content.parts:
                return ChatResponse(
                    message="لم يردّ النموذج بمحتوى.",
                    tool_calls=traces, table_data=last_table,
                )

            # Gather function_call parts vs text parts
            fn_calls = [p.function_call for p in candidate.content.parts if p.function_call]
            text_parts = [p.text for p in candidate.content.parts if p.text]

            if fn_calls:
                # Echo back assistant content with the function calls
                contents.append(candidate.content)

                # Execute each tool and add a single user-role Content with all responses
                response_parts = []
                for fc in fn_calls:
                    name = fc.name
                    args = dict(fc.args) if fc.args else {}
                    logger.info(f"[Gemini] tool: {name}({args})")
                    result = await execute_tool(name, args, erpnext_client)
                    last_table = _maybe_use_as_table(last_table, name, result)
                    traces.append(ToolCallTrace(
                        tool_name=name, arguments=args, result_summary=_summarize(result),
                    ))
                    response_parts.append(
                        gtypes.Part(
                            function_response=gtypes.FunctionResponse(
                                name=name,
                                response={"result": result},
                            )
                        )
                    )
                contents.append(gtypes.Content(role="user", parts=response_parts))
                continue

            # Final text answer
            text = "\n".join(t for t in text_parts if t)
            return ChatResponse(message=text, tool_calls=traces, table_data=last_table)

        return ChatResponse(
            message="عذراً، لم أتمكن من إكمال الطلب.",
            tool_calls=traces, table_data=last_table,
        )


_MEMORY_HEADERS = {
    "ar": "## معلومات يتذكّرها المستخدم (مهمّة)",
    "en": "## User-remembered facts (important)",
    "ckb": "## زانیاری بەکارهێنەر کە بیری دێتەوە (گرنگ)",
}


def _format_user_memory(entries: list[str], lang: str) -> str:
    """Render the user-curated memory list as a prompt section.

    Empty list → empty string (nothing prepended).
    Otherwise we add a clearly headed bullet list so the LLM treats
    these as durable user-supplied facts, not as instructions. They go
    at the end of the business-context block.
    """
    cleaned = [e.strip() for e in entries if e and e.strip()]
    if not cleaned:
        return ""
    header = _MEMORY_HEADERS.get(lang) or _MEMORY_HEADERS["ar"]
    bullets = "\n".join(f"- {e}" for e in cleaned[:20])
    return f"\n\n{header}\n{bullets}"


def _summarize(result: Any) -> str:
    if isinstance(result, list):
        return f"تم جلب {len(result)} سجل"
    if isinstance(result, dict):
        if "error" in result:
            return f"خطأ: {result['error']}"
        return f"تم — {len(result)} حقل"
    return str(result)[:80]
