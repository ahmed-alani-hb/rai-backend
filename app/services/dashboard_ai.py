"""Dashboard generator — asks AI to compose a personalized layout.

Uses Gemini if available (free tier), falls back to Claude, then to a static
template. The dashboard generation only needs ONE prompt — no agent loop —
so Gemini's free tier easily covers this for most usage.
"""
import json
from datetime import date
from typing import Any

from anthropic import AsyncAnthropic
from loguru import logger

from app.core.config import get_settings
from app.models.dashboard import DashboardCard, DashboardSpec
from app.prompts.dashboard_ar import build_dashboard_prompt, build_modify_prompt
from app.services.erpnext_client import ERPNextClient
from app.services.tools import execute_tool

settings = get_settings()


# ───────────────────────────────────────────────────────────────────────
# Card repair / validation
#
# Llama and (less often) Claude sometimes emit `tool_name=get_list`
# without the required `doctype` arg, producing cards that error at
# refresh time with "get_list يحتاج اسم doctype". When that happens we
# either:
#   (a) replace the bogus call with the right specialized tool when
#       the title clearly maps to one (suppliers/customers/items/etc.),
#   (b) infer a doctype from the title's keywords as a last resort.
# Cards we can't fix are dropped — better than showing a broken card.
# ───────────────────────────────────────────────────────────────────────

# Maps title-keyword regex → (preferred specialized tool, fallback doctype).
# Order matters: most specific patterns first.
_TITLE_REPAIRS: list[tuple[str, str | None, str]] = [
    # Pattern (case-insensitive substring), preferred tool, doctype
    ("supplier",        "get_top_suppliers",            "Supplier"),
    ("موردين",          "get_top_suppliers",            "Supplier"),
    ("موردي",           "get_top_suppliers",            "Supplier"),
    ("vendor",          "get_top_suppliers",            "Supplier"),
    ("customer",        "get_top_customers",            "Customer"),
    ("عملاء",           "get_top_customers",            "Customer"),
    ("کڕیار",           "get_top_customers",            "Customer"),
    ("unpaid",          "get_unpaid_invoices",          "Sales Invoice"),
    ("غير مدفوع",       "get_unpaid_invoices",          "Sales Invoice"),
    ("نەدراو",          "get_unpaid_invoices",          "Sales Invoice"),
    ("low stock",       "get_low_stock_items",          "Item"),
    ("مخزون",           "get_low_stock_items",          "Item"),
    ("inventory",       "get_low_stock_items",          "Item"),
    ("sales order",     "get_open_sales_orders",        "Sales Order"),
    ("أمر بيع",         "get_open_sales_orders",        "Sales Order"),
    ("purchase invoice","get_recent_purchase_invoices", "Purchase Invoice"),
    ("فاتورة شراء",     "get_recent_purchase_invoices", "Purchase Invoice"),
    # Pure-doctype fallbacks (no specialized tool — keep get_list with doctype)
    ("item",            None,                            "Item"),
    ("صنف",             None,                            "Item"),
    ("warehouse",       None,                            "Warehouse"),
    ("مخزن",            None,                            "Warehouse"),
]


def _repair_card_dict(c: dict[str, Any]) -> dict[str, Any] | None:
    """Fix obvious mistakes in an AI-emitted card dict, or return None
    to drop it. Currently handles missing-doctype on get_list."""
    tool_name = c.get("tool_name") or ""
    tool_args = dict(c.get("tool_args") or {})

    if tool_name == "get_list" and not tool_args.get("doctype"):
        title = (c.get("title_ar") or "").lower()
        for keyword, preferred_tool, doctype in _TITLE_REPAIRS:
            if keyword.lower() in title:
                if preferred_tool:
                    logger.info(
                        f"[card-repair] '{c.get('title_ar')}' "
                        f"→ {preferred_tool} (was get_list w/o doctype)"
                    )
                    c["tool_name"] = preferred_tool
                    # Keep `limit` if AI set it, drop other get_list-only args.
                    new_args = {}
                    if "limit" in tool_args:
                        new_args["limit"] = tool_args["limit"]
                    c["tool_args"] = new_args
                else:
                    logger.info(
                        f"[card-repair] '{c.get('title_ar')}' "
                        f"→ get_list+doctype={doctype}"
                    )
                    tool_args["doctype"] = doctype
                    tool_args.setdefault("limit", 10)
                    c["tool_args"] = tool_args
                return c
        # Couldn't infer — drop rather than ship a broken card.
        logger.warning(
            f"[card-repair] dropping '{c.get('title_ar')}' "
            f"— get_list without doctype and no matching keyword"
        )
        return None

    return c


def _safe_cards(raw_cards: list[Any]) -> list[DashboardCard]:
    """Build a list of DashboardCard from AI-emitted dicts, dropping
    or repairing entries that would otherwise blow up at render time."""
    out: list[DashboardCard] = []
    for c in raw_cards or []:
        if not isinstance(c, dict):
            continue
        repaired = _repair_card_dict(dict(c))
        if repaired is None:
            continue
        try:
            out.append(DashboardCard(**repaired))
        except Exception as e:
            logger.warning(
                f"[card-repair] dropping invalid card "
                f"{repaired.get('id')!r}: {e}"
            )
    return out


# Same shape as compose_dashboard but semantically used for incremental edits.
UPDATE_DASHBOARD_TOOL = {
    "name": "update_dashboard",
    "description": "Returns the COMPLETE updated dashboard layout after applying the user's modification request. Include ALL cards that should appear in the new dashboard (kept, modified, and added). Cards omitted from the list are removed.",
}


COMPOSE_DASHBOARD_TOOL = {
    "name": "compose_dashboard",
    "description": "Returns the dashboard layout. You MUST call this exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "greeting_ar": {"type": "string"},
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title_ar": {"type": "string"},
                        "subtitle_ar": {"type": "string"},
                        "icon": {"type": "string"},
                        "color": {"type": "string"},
                        "card_type": {
                            "type": "string",
                            "enum": ["kpi", "list", "table", "chart_bar", "chart_pie"],
                        },
                        "span": {"type": "integer", "enum": [1, 2]},
                        "tool_name": {"type": "string"},
                        # Gemini rejects bare {"type": "object"} without properties.
                        # We declare common keys and accept additional ones.
                        "tool_args": {
                            "type": "object",
                            "properties": {
                                "limit": {"type": "integer"},
                                "threshold": {"type": "integer"},
                                "date_from": {"type": "string"},
                                "date_to": {"type": "string"},
                                "territory": {"type": "string"},
                                "customer_filter": {"type": "string"},
                            },
                        },
                        "drilldown_prompt_ar": {"type": "string"},
                    },
                    "required": ["id", "title_ar", "card_type", "tool_name"],
                },
            },
        },
        "required": ["greeting_ar", "cards"],
    },
}


async def _fetch_data_summary(erp: ERPNextClient | None) -> dict[str, Any]:
    """Quick reconnaissance of what data exists, so AI can pick relevant cards.

    Each lookup is wrapped in try/except so a missing permission on one
    doctype doesn't blow up the whole summary.
    """
    if erp is None:
        return {}

    summary: dict[str, Any] = {}
    probes = [
        ("customers", "Customer", None),
        ("suppliers", "Supplier", None),
        ("items", "Item", None),
        ("unpaid_invoices", "Sales Invoice",
         [["status", "in", ["Unpaid", "Overdue", "Partly Paid"]], ["docstatus", "=", 1]]),
        ("paid_invoices_30d", "Sales Invoice",
         [["status", "=", "Paid"], ["posting_date", ">=", _days_ago(30)]]),
        ("purchase_invoices_30d", "Purchase Invoice",
         [["posting_date", ">=", _days_ago(30)]]),
        ("sales_orders_open", "Sales Order", [["status", "=", "To Deliver and Bill"]]),
    ]
    import asyncio
    async def probe(name, doctype, filters):
        try:
            count = await erp.get_count(doctype, filters=filters)
            return name, count
        except Exception:
            return name, None

    results = await asyncio.gather(*[probe(*p) for p in probes])
    for name, count in results:
        if count is not None:
            summary[name] = count
    return summary


def _days_ago(n: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


async def generate_dashboard(
    user_full_name: str,
    roles: list[str],
    erp: ERPNextClient | None = None,
    lang: str = "ar",
) -> DashboardSpec:
    """Try Gemini first (free), then Claude, then static fallback.

    If `erp` is provided, we pre-fetch a quick data summary so the AI can
    pick cards that match what data actually exists.

    `lang` is appended to the prompt as a directive: "Respond entirely in
    English/Sorani/Arabic so card titles match the UI." The AI tool
    schema field is still `title_ar` for backward compat — but the AI
    fills it with the requested language. Renaming to `title` is a
    follow-up that needs Flutter coordination.
    """
    data_summary = await _fetch_data_summary(erp)
    prompt = build_dashboard_prompt(
        user_full_name, roles, str(date.today()), data_summary=data_summary,
    )
    if lang and lang != "ar":
        # Tack a language directive on the end so the LLM produces the
        # whole spec in the requested language without needing per-locale
        # full prompt rewrites.
        lang_label = {"en": "English", "ckb": "Sorani Kurdish (کوردی)"}.get(
            lang, lang
        )
        prompt += (
            f"\n\n## LANGUAGE OVERRIDE\n"
            f"Reply in {lang_label} ONLY. Every value of `greeting_ar`, "
            f"`title_ar`, `subtitle_ar`, `drilldown_prompt_ar` MUST be "
            f"in {lang_label}, regardless of the field name. The field "
            f"names stay the same for backwards compatibility, but the "
            f"VALUES are in {lang_label}."
        )

    # 1) Try Gemini
    if settings.gemini_api_key:
        try:
            spec = await _generate_with_gemini(prompt, user_full_name, roles)
            if spec:
                return spec
        except Exception as e:
            logger.warning(f"Gemini dashboard failed: {e}, falling back")

    # 2) Try Claude
    if settings.anthropic_api_key:
        try:
            spec = await _generate_with_claude(prompt, user_full_name, roles)
            if spec:
                return spec
        except Exception as e:
            logger.warning(f"Claude dashboard failed: {e}, falling back")

    # 3) Static fallback
    return _fallback_dashboard(user_full_name, roles)


async def _generate_with_claude(prompt: str, user_full_name: str, roles: list[str]) -> DashboardSpec | None:
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2048,
        system=prompt,
        tools=[COMPOSE_DASHBOARD_TOOL],
        tool_choice={"type": "tool", "name": "compose_dashboard"},
        messages=[{"role": "user", "content": "ابنِ لي لوحة التحكم الخاصة بي الآن."}],
    )
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_use:
        return None
    spec_data = tool_use.input
    cards = _safe_cards(spec_data.get("cards", []))
    return DashboardSpec(
        title_ar="لوحة التحكم",
        greeting_ar=spec_data.get("greeting_ar", f"أهلاً {user_full_name}"),
        cards=cards,
        generated_for_roles=roles[:15],
        ai_provider="claude",
    )


async def _generate_with_gemini(prompt: str, user_full_name: str, roles: list[str]) -> DashboardSpec | None:
    try:
        from google import genai  # type: ignore
        from google.genai import types as gtypes  # type: ignore
    except ImportError:
        logger.info("google-genai not installed; dashboard skipping Gemini path")
        return None

    client = genai.Client(api_key=settings.gemini_api_key)
    tool = gtypes.Tool(function_declarations=[
        gtypes.FunctionDeclaration(
            name=COMPOSE_DASHBOARD_TOOL["name"],
            description=COMPOSE_DASHBOARD_TOOL["description"],
            parameters_json_schema=COMPOSE_DASHBOARD_TOOL["input_schema"],
        )
    ])

    resp = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=[
            gtypes.Content(
                role="user",
                parts=[gtypes.Part(text="ابنِ لي لوحة التحكم الخاصة بي الآن.")],
            )
        ],
        config=gtypes.GenerateContentConfig(
            system_instruction=prompt,
            tools=[tool],
            tool_config=gtypes.ToolConfig(
                function_calling_config=gtypes.FunctionCallingConfig(
                    mode="ANY",  # force a function call
                    allowed_function_names=["compose_dashboard"],
                )
            ),
            temperature=0.3,
        ),
    )

    candidate = resp.candidates[0] if resp.candidates else None
    if not candidate or not candidate.content or not candidate.content.parts:
        return None
    fn_call = next(
        (p.function_call for p in candidate.content.parts if p.function_call),
        None,
    )
    if not fn_call:
        return None
    spec_data = dict(fn_call.args) if fn_call.args else {}
    cards = _safe_cards(spec_data.get("cards", []))
    return DashboardSpec(
        title_ar="لوحة التحكم",
        greeting_ar=spec_data.get("greeting_ar", f"أهلاً {user_full_name}"),
        cards=cards,
        generated_for_roles=roles[:15],
        ai_provider="gemini",
    )


# Phrase table for card subtitles. The legacy `card.subtitle_ar` field
# name is kept so existing serialization stays backward compatible — we
# just write the requested locale's phrasing into it.
_CARD_PHRASES: dict[str, dict[str, str]] = {
    "ar": {
        "total_with_amount": "بإجمالي {amount} {curr}",
        "mixed_currencies": "بـ {n} عملات مختلفة",
        "no_data": "لا توجد بيانات",
        "no_invoices": "لا توجد فواتير",
        "unknown_data": "(بيانات غير معروفة)",
        "no_response": "(لا يوجد رد)",
        "error_value": "خطأ",
        "monthly_growth": "{arrow} {growth:+.1f}% شهرياً",
        "total_expenses": "إجمالي المصاريف",
        "gross_with_margin": "ربح إجمالي {gp} • هامش {margin:.1f}%",
        "currency_invoices": "{curr} • {n} فاتورة",
        "plus_other_currencies": " + {n} عملات أخرى",
        "n_invoices": "{n} فاتورة",
        "n_accounts": "{n} حساب",
        "n_suppliers": "{n} مورد",
        "n_customers": "{n} عميل",
        "margin_with_revenue": "هامش {margin:.1f}% • إيراد {rev}",
    },
    "en": {
        "total_with_amount": "Total {amount} {curr}",
        "mixed_currencies": "{n} different currencies",
        "no_data": "No data",
        "no_invoices": "No invoices",
        "unknown_data": "(unknown data)",
        "no_response": "(no response)",
        "error_value": "Error",
        "monthly_growth": "{arrow} {growth:+.1f}% MoM",
        "total_expenses": "Total expenses",
        "gross_with_margin": "Gross {gp} • {margin:.1f}% margin",
        "currency_invoices": "{curr} • {n} invoices",
        "plus_other_currencies": " + {n} more currencies",
        "n_invoices": "{n} invoices",
        "n_accounts": "{n} accounts",
        "n_suppliers": "{n} suppliers",
        "n_customers": "{n} customers",
        "margin_with_revenue": "{margin:.1f}% margin • {rev} revenue",
    },
    "ckb": {
        "total_with_amount": "کۆی گشتی {amount} {curr}",
        "mixed_currencies": "{n} دراوی جیاواز",
        "no_data": "هیچ زانیارییەک نییە",
        "no_invoices": "هیچ پسوولەیەک نییە",
        "unknown_data": "(زانیاری نەناسراو)",
        "no_response": "(وەڵامی نییە)",
        "error_value": "هەڵە",
        "monthly_growth": "{arrow} {growth:+.1f}% مانگانە",
        "total_expenses": "کۆی خەرجی",
        "gross_with_margin": "قازانجی گشتی {gp} • هامش {margin:.1f}%",
        "currency_invoices": "{curr} • {n} پسوولە",
        "plus_other_currencies": " + {n} دراوی تر",
        "n_invoices": "{n} پسوولە",
        "n_accounts": "{n} هەژمار",
        "n_suppliers": "{n} دابینکەر",
        "n_customers": "{n} کڕیار",
        "margin_with_revenue": "هامش {margin:.1f}% • داهات {rev}",
    },
}


def _phrase(lang: str, key: str, **kwargs) -> str:
    """Look up a localized phrase, falling back to Arabic."""
    table = _CARD_PHRASES.get(lang) or _CARD_PHRASES["ar"]
    template = table.get(key) or _CARD_PHRASES["ar"].get(key, key)
    return template.format(**kwargs)


async def refresh_card(
    card: DashboardCard,
    erp: ERPNextClient,
    lang: str = "ar",
) -> DashboardCard:
    """Fetch fresh data for a single dashboard card.

    Always sets a visible value/subtitle so the UI never shows a blank card.
    `lang` selects the phrasing used for value/subtitle text written into
    the card — `subtitle_ar` keeps its legacy field name but holds whatever
    locale was requested.
    """
    logger.info(f"refresh_card[{card.id}] tool={card.tool_name} args={card.tool_args}")
    try:
        result = await execute_tool(card.tool_name, card.tool_args, erp)
        logger.info(
            f"refresh_card[{card.id}] result_type={type(result).__name__} "
            f"size={len(result) if hasattr(result, '__len__') else 'n/a'}"
        )

        if isinstance(result, list):
            if card.card_type == "kpi":
                card.value = str(len(result))
                currencies = {r.get("currency") for r in result if r.get("currency")}
                if len(currencies) <= 1:
                    amounts = [r.get("outstanding_amount") or r.get("grand_total") or 0 for r in result]
                    if any(amounts):
                        total = sum(amounts)
                        curr = currencies.pop() if currencies else ""
                        card.subtitle_ar = _phrase(
                            lang, "total_with_amount",
                            amount=_fmt_money(total), curr=curr,
                        ).strip()
                else:
                    card.subtitle_ar = _phrase(
                        lang, "mixed_currencies", n=len(currencies),
                    )
            else:
                card.rows = result[:10] if result else []
                if not result:
                    card.subtitle_ar = _phrase(lang, "no_data")
        elif isinstance(result, dict):
            if "error" in result:
                card.value = "—"
                card.subtitle_ar = result["error"][:80]
            # ── Executive tools that return a wrapped list ──
            elif "trend" in result and isinstance(result["trend"], list):
                # get_monthly_sales_trend
                card.rows = [
                    {"label": r.get("month", ""), "value": r.get("total_sales", 0)}
                    for r in result["trend"]
                ]
                if result.get("mom_growth_pct") is not None:
                    growth = result["mom_growth_pct"]
                    arrow = "▲" if growth >= 0 else "▼"
                    card.subtitle_ar = _phrase(
                        lang, "monthly_growth", arrow=arrow, growth=growth,
                    )
                if card.card_type == "kpi" and card.rows:
                    card.value = _fmt_money(card.rows[-1]["value"])
            elif "top_expenses" in result and isinstance(result["top_expenses"], list):
                # get_expense_breakdown
                card.rows = [
                    {"label": r.get("account", ""), "value": r.get("amount", 0),
                     "share_percent": r.get("share_percent", 0)}
                    for r in result["top_expenses"]
                ]
                total = result.get("total_expenses", 0)
                if card.card_type == "kpi":
                    card.value = _fmt_money(total)
                    card.subtitle_ar = _phrase(lang, "total_expenses")
            elif "headline" in result:
                # get_executive_summary — surface the most relevant KPI
                h = result["headline"]
                card.value = _fmt_money(h.get("revenue") or 0)
                gp = h.get("gross_profit")
                margin = h.get("gross_margin_pct")
                if gp is not None and margin is not None:
                    card.subtitle_ar = _phrase(
                        lang, "gross_with_margin",
                        gp=_fmt_money(gp), margin=margin,
                    )
            elif "total_cash_and_bank" in result:
                # get_cash_position — total + per-account list
                card.value = _fmt_money(result.get("total_cash_and_bank") or 0)
                accounts = result.get("accounts") or []
                if accounts:
                    card.rows = [
                        {"label": a.get("account", ""), "value": a.get("balance", 0)}
                        for a in accounts
                    ]
                    card.subtitle_ar = _phrase(lang, "n_accounts", n=len(accounts))
                elif result.get("note"):
                    # _get_company_currency may have returned the no-accounts note
                    card.subtitle_ar = result["note"][:80]
                else:
                    card.subtitle_ar = _phrase(lang, "no_data")
            elif "total_outstanding" in result and "by_supplier" in result:
                # get_payables_summary — supplier ranking + total
                card.value = _fmt_money(result.get("total_outstanding") or 0)
                by_sup = result.get("by_supplier") or []
                if by_sup:
                    card.rows = [
                        {"label": s.get("supplier_name") or s.get("supplier", ""),
                         "value": s.get("outstanding", 0)}
                        for s in by_sup
                    ]
                sup_count = result.get("supplier_count", len(by_sup))
                card.subtitle_ar = _phrase(lang, "n_suppliers", n=sup_count)
            elif "gross_profit" in result and "revenue" in result:
                # get_gross_profit — headline number is gross profit, with
                # the margin percentage as the subtitle.
                card.value = _fmt_money(result.get("gross_profit") or 0)
                margin = result.get("gross_margin_pct") or 0
                rev = result.get("revenue") or 0
                card.subtitle_ar = _phrase(
                    lang, "margin_with_revenue",
                    margin=margin, rev=_fmt_money(rev),
                )
            elif "top_customers" in result and "total_revenue" in result:
                # get_customer_profitability — total revenue + ranking list
                card.value = _fmt_money(result.get("total_revenue") or 0)
                top_cust = result.get("top_customers") or []
                if top_cust:
                    card.rows = [
                        {"label": c.get("customer", ""),
                         "value": c.get("revenue", 0),
                         "share_percent": c.get("share_percent", 0)}
                        for c in top_cust
                    ]
                cust_count = result.get("customer_count", len(top_cust))
                card.subtitle_ar = _phrase(lang, "n_customers", n=cust_count)
            elif "by_currency" in result:
                # Multi-currency sales summary
                bc = result["by_currency"]
                if bc:
                    primary = bc[0]
                    card.value = _fmt_money(primary.get("total_amount", 0))
                    text = _phrase(
                        lang, "currency_invoices",
                        curr=primary.get("currency", ""),
                        n=result.get("invoice_count", 0),
                    )
                    if len(bc) > 1:
                        text += _phrase(
                            lang, "plus_other_currencies", n=len(bc) - 1,
                        )
                    card.subtitle_ar = text
                else:
                    card.value = "0"
                    card.subtitle_ar = _phrase(lang, "no_invoices")
            elif "total_amount" in result:
                card.value = _fmt_money(result["total_amount"])
                card.subtitle_ar = _phrase(
                    lang, "n_invoices", n=result.get("invoice_count", 0),
                )
            else:
                card.value = "—"
                card.subtitle_ar = _phrase(lang, "unknown_data")
        elif isinstance(result, (int, float)):
            card.value = str(result)
        else:
            card.value = "—"
            card.subtitle_ar = _phrase(lang, "no_response")
    except Exception as e:
        logger.exception(f"refresh_card[{card.id}] failed")
        card.value = "—"
        card.subtitle_ar = _friendly_error(e, lang, card.tool_name)

    return card


# ───────────────────────────────────────────────────────────────────
# Error-message classifier — turn raw Frappe/ERPNext exceptions into
# human-readable, localized one-liners. The user is a non-technical
# manager; pasting `{"exception":"frappe.exceptions..."}` into the
# card subtitle is worse than useless.
# ───────────────────────────────────────────────────────────────────

# Tools that need the Accounts module / account-related role.
# When one of these fails with a permission error, point the user at
# the right ERPNext role rather than a generic "error".
_TOOLS_NEEDING_ACCOUNTS = {
    "get_cash_position",
    "get_payables_summary",
    "get_accounts_receivable",
    "get_general_ledger",
    "get_trial_balance",
    "get_balance_sheet",
    "get_profit_loss_report",
    "get_cash_flow_report",
    "get_gross_profit",
    "get_executive_summary",
    "get_expense_breakdown",
}

_ERR_PHRASES = {
    "ar": {
        "permission_accounts": "صلاحية محاسبة مطلوبة في ERPNext",
        "permission_generic": "صلاحيات غير كافية في ERPNext",
        "rate_limit": "تجاوز حد الطلبات اليومي",
        "timeout": "انتهت مهلة الطلب",
        "network": "فشل الاتصال بـ ERPNext",
        "not_found": "المستند غير موجود",
        "generic": "تعذّر تحميل البيانات",
    },
    "en": {
        "permission_accounts": "Needs Accounts role in ERPNext",
        "permission_generic": "Insufficient ERPNext permissions",
        "rate_limit": "Daily request limit exceeded",
        "timeout": "Request timed out",
        "network": "Couldn't reach ERPNext",
        "not_found": "Document not found",
        "generic": "Couldn't load data",
    },
    "ckb": {
        "permission_accounts": "ڕۆڵی Accounts پێویستە لە ERPNext",
        "permission_generic": "مۆڵەتی پێویست لە ERPNext نییە",
        "rate_limit": "سنووری ڕۆژانەی داواکاری بەسەرچوو",
        "timeout": "کاتی داواکاری بەسەرچوو",
        "network": "نەتوانرا پەیوەندی بکەیت بە ERPNext",
        "not_found": "بەڵگەنامە نەدۆزرایەوە",
        "generic": "نەتوانرا زانیاری باربکرێت",
    },
}


def _friendly_error(e: Exception, lang: str, tool_name: str) -> str:
    """Map a raw exception into a single localized line the user can act
    on. Falls back to a generic message for anything we don't recognize.
    """
    table = _ERR_PHRASES.get(lang) or _ERR_PHRASES["ar"]
    text = str(e).lower()

    # Permission / 417 / Frappe role errors. Frappe surfaces these as
    # "417 Expectation Failed" + a JSON body containing
    # frappe.exceptions.PermissionError or frappe.exceptions.ValidationError
    # ("not permitted").
    permission_signals = (
        "417",
        "permission",
        "not permitted",
        "permissionerror",
        "not allowed",
    )
    if any(sig in text for sig in permission_signals):
        if tool_name in _TOOLS_NEEDING_ACCOUNTS:
            return table["permission_accounts"]
        return table["permission_generic"]

    # Other common failure modes
    if "429" in text or "rate limit" in text or "quota" in text:
        return table["rate_limit"]
    if "timeout" in text or "timed out" in text:
        return table["timeout"]
    if "connection" in text or "network" in text or "unreachable" in text:
        return table["network"]
    if "404" in text or "doesnotexist" in text or "not found" in text:
        return table["not_found"]

    return table["generic"]


def _fmt_money(amount: float) -> str:
    if amount >= 1_000_000:
        return f"{amount/1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount/1_000:.1f}K"
    return f"{amount:.0f}"


async def modify_dashboard(
    current_spec: DashboardSpec,
    instruction_ar: str,
    user_full_name: str,
    roles: list[str],
) -> tuple[DashboardSpec, bool]:
    """Apply a natural-language modification to the current dashboard.

    Returns (new_spec, was_modified). If AI didn't return a valid spec,
    returns (current_spec, False) so the caller can show a "couldn't apply" hint.
    """
    # Serialize the current spec compactly for the prompt
    current_json = json.dumps(
        current_spec.model_dump(),
        ensure_ascii=False,
        indent=2,
    )
    prompt = build_modify_prompt(
        current_spec_json=current_json,
        instruction=instruction_ar,
        user_full_name=user_full_name,
        roles=roles,
        today=str(date.today()),
    )

    # Try Gemini first (free), then Claude
    if settings.gemini_api_key:
        try:
            new_spec = await _modify_with_gemini(prompt, user_full_name, roles)
            if new_spec and new_spec.cards:
                return new_spec, True
        except Exception as e:
            logger.warning(f"Gemini modify failed: {e}")

    if settings.anthropic_api_key:
        try:
            new_spec = await _modify_with_claude(prompt, user_full_name, roles)
            if new_spec and new_spec.cards:
                return new_spec, True
        except Exception as e:
            logger.warning(f"Claude modify failed: {e}")

    return current_spec, False


async def _modify_with_claude(prompt: str, user_full_name: str, roles: list[str]) -> DashboardSpec | None:
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    # Reuse the same schema shape as compose, but with a different name.
    update_tool = {
        "name": "update_dashboard",
        "description": UPDATE_DASHBOARD_TOOL["description"],
        "input_schema": COMPOSE_DASHBOARD_TOOL["input_schema"],
    }
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2048,
        system=prompt,
        tools=[update_tool],
        tool_choice={"type": "tool", "name": "update_dashboard"},
        messages=[{"role": "user", "content": "طبّق التعديل الآن."}],
    )
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_use:
        return None
    spec_data = tool_use.input
    cards = _safe_cards(spec_data.get("cards", []))
    return DashboardSpec(
        title_ar="لوحة التحكم",
        greeting_ar=spec_data.get("greeting_ar", f"أهلاً {user_full_name}"),
        cards=cards,
        generated_for_roles=roles[:15],
        ai_provider="claude",
    )


async def _modify_with_gemini(prompt: str, user_full_name: str, roles: list[str]) -> DashboardSpec | None:
    try:
        from google import genai  # type: ignore
        from google.genai import types as gtypes  # type: ignore
    except ImportError:
        return None

    client = genai.Client(api_key=settings.gemini_api_key)
    tool = gtypes.Tool(function_declarations=[
        gtypes.FunctionDeclaration(
            name="update_dashboard",
            description=UPDATE_DASHBOARD_TOOL["description"],
            parameters_json_schema=COMPOSE_DASHBOARD_TOOL["input_schema"],
        )
    ])
    resp = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=[
            gtypes.Content(role="user", parts=[gtypes.Part(text="طبّق التعديل الآن.")]),
        ],
        config=gtypes.GenerateContentConfig(
            system_instruction=prompt,
            tools=[tool],
            tool_config=gtypes.ToolConfig(
                function_calling_config=gtypes.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=["update_dashboard"],
                )
            ),
            temperature=0.3,
        ),
    )
    candidate = resp.candidates[0] if resp.candidates else None
    if not candidate or not candidate.content or not candidate.content.parts:
        return None
    fn_call = next(
        (p.function_call for p in candidate.content.parts if p.function_call),
        None,
    )
    if not fn_call:
        return None
    spec_data = dict(fn_call.args) if fn_call.args else {}
    # Gemini emits Mapping objects; coerce each to a real dict before
    # repair so _safe_cards can mutate freely.
    raw = [dict(c) for c in spec_data.get("cards", []) if c]
    cards = _safe_cards(raw)
    return DashboardSpec(
        title_ar="لوحة التحكم",
        greeting_ar=spec_data.get("greeting_ar", f"أهلاً {user_full_name}"),
        cards=cards,
        generated_for_roles=roles[:15],
        ai_provider="gemini",
    )


def _fallback_dashboard(user_full_name: str, roles: list[str]) -> DashboardSpec:
    return DashboardSpec(
        greeting_ar=f"أهلاً {user_full_name}",
        cards=[
            DashboardCard(
                id="unpaid", title_ar="الفواتير غير المدفوعة",
                icon="receipt_long", color="warning", card_type="kpi", span=1,
                tool_name="get_unpaid_invoices", tool_args={"limit": 50},
                drilldown_prompt_ar="أعطني تفاصيل الفواتير غير المدفوعة",
            ),
            DashboardCard(
                id="low_stock", title_ar="أصناف ناقصة",
                icon="inventory_2", color="danger", card_type="kpi", span=1,
                tool_name="get_low_stock_items", tool_args={"threshold": 10},
                drilldown_prompt_ar="ما هي الأصناف القاربة على النفاد؟",
            ),
            DashboardCard(
                id="top_customers", title_ar="أكبر العملاء",
                icon="people", color="info", card_type="list", span=2,
                tool_name="get_top_customers", tool_args={"limit": 5},
                drilldown_prompt_ar="من هم أكبر عملائي؟",
            ),
        ],
        generated_for_roles=roles[:15],
        ai_provider="fallback",
    )
