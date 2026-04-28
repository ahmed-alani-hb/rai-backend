"""Cheap-model query classifier — saves AI cost by routing intelligently.

Three tiers:
1. DETERMINISTIC — pattern matches a known shortcut (regex). Run Python directly,
   no AI. Examples: "كم عميل؟", "السلام عليكم".
2. SIMPLE — needs one ERPNext tool call, Gemini Flash answers (free tier).
3. COMPLEX — multi-step reasoning, Claude answers (paid).

Classification done by Llama 3.3 70B via Groq (free tier, ~150ms latency).
The classifier itself is sometimes skipped if the regex layer matches first.
"""
import re
from dataclasses import dataclass
from typing import Literal, Optional

from loguru import logger
from openai import AsyncOpenAI

from app.core.config import get_settings

settings = get_settings()

Tier = Literal["deterministic", "simple", "complex"]


@dataclass
class Classification:
    tier: Tier
    # For deterministic tier, which shortcut to run
    shortcut: Optional[str] = None
    # For deterministic shortcuts, args extracted from the query
    args: dict = None  # type: ignore[assignment]
    # Reason for routing (for logs/debug)
    reason: str = ""


# ============================================================================
# Layer 1: Regex shortcuts — instant, free, no AI involved
# ============================================================================

_SHORTCUT_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    # Greetings / pleasantries
    (re.compile(r"^\s*(السلام\s+عليكم|مرحبا|أهلا|اهلا|hi|hello)\s*$", re.IGNORECASE), "greeting", {}),
    (re.compile(r"^\s*(شكرا|شكراً|thanks|thank you)\s*$", re.IGNORECASE), "thanks", {}),

    # Time / date — never needs AI, just answer from server clock.
    # Loose patterns to tolerate Arabic typos (ساعة / ساهة / سائة, etc.)
    (re.compile(r"^\s*كم\s+ال?س[اأآ]?[عهئ]?[ةه]?\b", re.IGNORECASE), "current_time", {}),
    (re.compile(r"^\s*كم\s+الوقت", re.IGNORECASE), "current_time", {}),
    (re.compile(r"^\s*(ما|اي|أي)\s+(هو|هي)?\s*(التاريخ|اليوم)", re.IGNORECASE), "current_date", {}),
    (re.compile(r"^\s*(what'?s? the time|current time|the time)", re.IGNORECASE), "current_time", {}),
    (re.compile(r"^\s*(what'?s? the date|today'?s? date|the date)", re.IGNORECASE), "current_date", {}),

    # Simple counts
    (re.compile(r"^\s*كم\s+(عميل|عملاء|customer|customers)(\s|\?|؟|$)", re.IGNORECASE), "count_customers", {}),
    (re.compile(r"^\s*كم\s+(مورد|موردين|supplier|suppliers)(\s|\?|؟|$)", re.IGNORECASE), "count_suppliers", {}),
    (re.compile(r"^\s*كم\s+(صنف|أصناف|item|items|منتج)(\s|\?|؟|$)", re.IGNORECASE), "count_items", {}),
    (re.compile(r"^\s*كم\s+(فاتورة|فواتير|invoice|invoices)\s+غير\s+(مدفوع|مدفوعة)", re.IGNORECASE), "count_unpaid", {}),
]


def _try_regex_shortcut(query: str) -> Optional[Classification]:
    """Returns a deterministic classification if the query matches a known
    shortcut pattern. None means we need to think harder."""
    text = query.strip()
    for pattern, shortcut, args in _SHORTCUT_PATTERNS:
        if pattern.search(text):
            return Classification(
                tier="deterministic",
                shortcut=shortcut,
                args=args,
                reason=f"regex matched {shortcut}",
            )
    return None


# ============================================================================
# Layer 2: AI classifier (Groq Llama)
# ============================================================================

CLASSIFIER_SYSTEM = """You classify Arabic ERP queries into one tier.

Output ONLY a single word: simple OR complex. No explanation.

simple = One ERPNext tool call answers it. Examples:
  "أعطني الفواتير غير المدفوعة" → simple (one tool: get_unpaid_invoices)
  "ما هي الأصناف الناقصة" → simple (one tool: get_low_stock_items)
  "أكبر 5 عملاء" → simple
  "ملخص مبيعات الشهر" → simple

complex = Needs multiple tool calls, comparison, calculation, or analysis. Examples:
  "قارن مبيعات هذا الشهر بالشهر الماضي" → complex (two summaries + comparison)
  "أعطني أكبر عميل من المشتريات وأكبر عميل من البيع" → complex (two queries)
  "حلل أداء فرع الموصل" → complex (multiple aggregations)
  "ما هي اتجاهات المبيعات في الربع الأخير" → complex (trend analysis)

When unsure, output: simple (we'd rather call a cheap fast model)
"""


async def _ai_classify(query: str) -> Classification:
    """Use Groq Llama 3.3 to classify. ~150ms latency, free tier."""
    if not settings.groq_api_key:
        # No Groq key → skip classification, default to simple (Gemini handles)
        return Classification(tier="simple", reason="no groq key, defaulting to simple")

    try:
        client = AsyncOpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
        )
        resp = await client.chat.completions.create(
            model=settings.groq_classifier_model,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": query},
            ],
            max_tokens=4,
            temperature=0,
        )
        verdict = (resp.choices[0].message.content or "").strip().lower()
        if "complex" in verdict:
            return Classification(tier="complex", reason=f"groq said: {verdict}")
        return Classification(tier="simple", reason=f"groq said: {verdict}")
    except Exception as e:
        logger.warning(f"classifier failed, defaulting to simple: {e}")
        return Classification(tier="simple", reason=f"classifier error: {e}")


# ============================================================================
# Public API
# ============================================================================

async def classify_query(query: str) -> Classification:
    """Top-level classifier. Tries regex first (instant), falls back to AI."""
    shortcut = _try_regex_shortcut(query)
    if shortcut:
        return shortcut

    return await _ai_classify(query)


# ============================================================================
# Deterministic shortcut handlers — execute without any AI
# ============================================================================

async def run_shortcut(
    shortcut: str,
    args: dict,
    erpnext_client,
    user_full_name: str,
) -> str:
    """Run a deterministic shortcut, return Arabic response text.
    No AI involvement — pure Python.
    """
    if shortcut == "greeting":
        return f"أهلاً بك يا {user_full_name}! 🤖\nكيف أستطيع مساعدتك في بياناتك؟"

    if shortcut == "thanks":
        return "العفو! متى احتجت أي معلومة، أنا هنا. 🙌"

    if shortcut == "current_time":
        from datetime import datetime
        now = datetime.now()
        # Format Arabic-friendly time (12-hour with AM/PM as صباحاً/مساءً)
        hour_24 = now.hour
        hour_12 = hour_24 % 12 or 12
        ampm = "صباحاً" if hour_24 < 12 else "مساءً"
        return f"🕒 الساعة الآن **{hour_12}:{now.minute:02d}** {ampm} (اليوم {now.strftime('%Y-%m-%d')})."

    if shortcut == "current_date":
        from datetime import datetime
        now = datetime.now()
        ar_months = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                     "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
        return f"📅 اليوم هو **{now.day} {ar_months[now.month-1]} {now.year}**."

    if shortcut == "count_customers":
        n = await erpnext_client.get_count("Customer")
        return f"📊 لديك **{n}** عميل مسجّل في النظام."

    if shortcut == "count_suppliers":
        n = await erpnext_client.get_count("Supplier")
        return f"📊 لديك **{n}** مورّد مسجّل في النظام."

    if shortcut == "count_items":
        n = await erpnext_client.get_count("Item")
        return f"📦 لديك **{n}** صنف في كتالوج المنتجات."

    if shortcut == "count_unpaid":
        n = await erpnext_client.get_count(
            "Sales Invoice",
            filters=[
                ["status", "in", ["Unpaid", "Overdue", "Partly Paid"]],
                ["docstatus", "=", 1],
            ],
        )
        return f"⚠️ لديك **{n}** فاتورة بحاجة للتحصيل."

    return "—"
