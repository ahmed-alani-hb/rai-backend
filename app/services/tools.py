"""Tool definitions for the AI."""
from typing import Any

TOOL_DEFINITIONS = [
    {
        "name": "get_unpaid_invoices",
        "description": "جلب الفواتير غير المدفوعة (Unpaid أو Overdue أو Partly Paid). "
                       "يدعم تصفية حسب اسم العميل والمنطقة والتواريخ.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_filter": {"type": "string", "description": "جزء من اسم العميل"},
                "territory": {"type": "string", "description": "اسم المنطقة"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    },
    {
        "name": "get_low_stock_items",
        "description": "جلب الأصناف التي اقترب مخزونها من النفاد.",
        "parameters": {
            "type": "object",
            "properties": {
                "threshold": {"type": "integer", "default": 10},
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    },
    {
        "name": "get_sales_summary",
        "description": "ملخص المبيعات بين تاريخين: المجموع، المدفوع، المتبقي، عدد الفواتير.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_top_customers",
        "description": "قائمة أكبر العملاء (Customer doctype).",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    {
        "name": "get_top_suppliers",
        "description": "قائمة الموردين (Supplier doctype).",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    {
        "name": "get_open_sales_orders",
        "description": "أوامر البيع المفتوحة (Sales Order بحالة To Deliver and Bill / To Bill / To Deliver).",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    {
        "name": "get_recent_purchase_invoices",
        "description": "فواتير الشراء (Purchase Invoice) خلال الأيام الماضية.",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 30},
                "limit": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "get_profit_loss_report",
        "description": "تقرير الأرباح والخسائر (Profit and Loss Statement) — يجلب الإيرادات والمصروفات وصافي الربح بين تاريخين. استخدمها لأي سؤال عن الربح، الخسارة، أو الأداء المالي.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "company": {"type": "string", "description": "اسم الشركة (اختياري)"},
                "periodicity": {
                    "type": "string",
                    "enum": ["Monthly", "Quarterly", "Half-Yearly", "Yearly"],
                    "default": "Monthly",
                },
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_balance_sheet",
        "description": "تقرير الميزانية العامة (Balance Sheet) — الأصول والخصوم وحقوق الملكية بتاريخ محدّد.",
        "parameters": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "description": "تاريخ بصيغة YYYY-MM-DD"},
                "company": {"type": "string"},
            },
            "required": ["as_of"],
        },
    },
    {
        "name": "get_accounts_receivable",
        "description": "ملخص حسابات العملاء المدينة (Accounts Receivable) — كم يدين كل عميل وعمر الديون (0-30، 30-60، إلخ).",
        "parameters": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "as_of": {"type": "string", "description": "YYYY-MM-DD، افتراضياً اليوم"},
            },
            "required": [],
        },
    },
    {
        "name": "get_general_ledger",
        "description": "دفتر الأستاذ العام (General Ledger) — تفاصيل الحركات المحاسبية بين تاريخين، اختيارياً لحساب معيّن.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "account": {"type": "string", "description": "اسم الحساب (اختياري)"},
                "company": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_trial_balance",
        "description": "ميزان المراجعة (Trial Balance) — أرصدة كل الحسابات (مدين/دائن).",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_cash_flow_report",
        "description": "تقرير التدفق النقدي (Cash Flow) — حركة النقد المُصنّفة (تشغيلية، استثمارية، تمويلية).",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_customer_profitability",
        "description": "ربحية العملاء — إيرادات كل عميل وحصته من المبيعات.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_item_profitability",
        "description": "ربحية الأصناف — إيرادات كل صنف وهامش الربح إن توفّرت بيانات التكلفة.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["date_from", "date_to"],
        },
    },
    # ──────────────────────────────────────────────────────────────
    # Executive / C-level tools — use these for owner-facing questions.
    # All amounts are returned in the company's base currency.
    # ──────────────────────────────────────────────────────────────
    {
        "name": "get_gross_profit",
        "description": "الربح الإجمالي والصافي + الهامش بين تاريخين.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_monthly_sales_trend",
        "description": "مبيعات آخر N شهر + نمو شهري.",
        "parameters": {
            "type": "object",
            "properties": {
                "months": {"type": "integer", "default": 12},
                "company": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_expense_breakdown",
        "description": "أكبر بنود المصروفات بين تاريخين.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_cash_position",
        "description": "أرصدة النقد والبنك حالياً.",
        "parameters": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "as_of": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_payables_summary",
        "description": "كم ندين للموردين (AP) مرتباً تنازلياً.",
        "parameters": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "as_of": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "get_executive_summary",
        "description": "نظرة شاملة بطلب واحد: إيراد + ربح + هامش + نقد + AR + AP + أكبر 3 عملاء.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "company": {"type": "string"},
            },
            "required": ["date_from", "date_to"],
        },
    },
    {
        "name": "get_list",
        "description": "أداة عامة لجلب قائمة من أي DocType في ERPNext.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctype": {"type": "string"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "أسماء الحقول المراد جلبها",
                },
                "filters": {
                    "type": "array",
                    # Gemini rejects array without items. We describe the
                    # nested filter shape via description rather than schema
                    # because it's tuple-like (heterogeneous types per index).
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": "قائمة فلاتر كل واحد بصيغة [field, operator, value], مثلاً [['status','=','Paid']]",
                },
                "limit": {"type": "integer", "default": 20},
                "order_by": {
                    "type": "string",
                    "description": "مثل 'creation desc' أو 'posting_date desc'",
                },
            },
            "required": ["doctype"],
        },
    },
]


def to_openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_DEFINITIONS
    ]


def _loosen_integers(schema: Any) -> Any:
    """Recursively replace ``"type": "integer"`` with ``["integer","string"]``.

    Groq's Llama tends to emit numeric arguments as strings (``"limit": "10"``).
    Groq's server validates the function-call arguments against the tool
    schema and rejects strict-int with ``tool_use_failed``. By accepting
    both at the schema level, we let Groq pass it through; ``execute_tool``
    coerces the value to an int on our side before invoking the function.
    """
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k == "type" and v == "integer":
                out[k] = ["integer", "string"]
            else:
                out[k] = _loosen_integers(v)
        return out
    if isinstance(schema, list):
        return [_loosen_integers(item) for item in schema]
    return schema


def to_groq_tools() -> list[dict[str, Any]]:
    """OpenAI-compatible tools but with relaxed integer types.

    Groq is OpenAI-compatible at the wire format but stricter about type
    matching during tool-call validation, while Llama is laxer about
    emitting JSON types. The combination causes ``tool_use_failed`` 400s.
    Loosening ``integer`` to ``[integer, string]`` neutralizes the mismatch.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": _loosen_integers(t["parameters"]),
            },
        }
        for t in TOOL_DEFINITIONS
    ]


def to_claude_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in TOOL_DEFINITIONS
    ]


def to_gemini_tools() -> list[Any]:
    """Convert to Gemini FunctionDeclaration objects wrapped in Tool.

    Raises a clear error if google-genai isn't installed.
    """
    try:
        from google.genai import types as gtypes  # lazy import
    except ImportError as e:
        raise RuntimeError(
            "google-genai is required for Gemini provider. Install with: "
            "pip install google-genai"
        ) from e

    declarations = [
        gtypes.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters_json_schema=t["parameters"],
        )
        for t in TOOL_DEFINITIONS
    ]
    return [gtypes.Tool(function_declarations=declarations)]


async def execute_tool(name: str, args: dict[str, Any], erpnext_client) -> Any:
    """Dispatch a tool call. Sanitizes args, caches results, and converts
    errors to friendly Arabic messages.

    Caching: identical (tool_name, args, erp_url, api_key) within 5 minutes
    returns the cached value. Saves Frappe load and AI tool-roundtrip cost.
    """
    from app.services.cache import tool_cache

    args = args or {}

    # Whitelist allowed args per tool so AI hallucinations don't crash Python.
    # Anything not in this list gets dropped silently.
    allowed = {
        "get_unpaid_invoices": {"customer_filter", "territory", "date_from", "date_to", "limit"},
        "get_low_stock_items": {"threshold", "limit"},
        "get_sales_summary": {"date_from", "date_to"},
        "get_top_customers": {"limit"},
        "get_top_suppliers": {"limit"},
        "get_open_sales_orders": {"limit"},
        "get_recent_purchase_invoices": {"days", "limit"},
        "get_list": {"doctype", "fields", "filters", "limit", "order_by"},
        "get_profit_loss_report": {"date_from", "date_to", "company", "periodicity"},
        "get_balance_sheet": {"as_of", "company"},
        "get_accounts_receivable": {"company", "as_of"},
        "get_general_ledger": {"date_from", "date_to", "account", "company", "limit"},
        "get_trial_balance": {"date_from", "date_to", "company"},
        "get_cash_flow_report": {"date_from", "date_to", "company"},
        "get_customer_profitability": {"date_from", "date_to", "company", "limit"},
        "get_item_profitability": {"date_from", "date_to", "company", "limit"},
        "get_gross_profit": {"date_from", "date_to", "company"},
        "get_monthly_sales_trend": {"months", "company"},
        "get_expense_breakdown": {"date_from", "date_to", "company", "limit"},
        "get_cash_position": {"company", "as_of"},
        "get_payables_summary": {"company", "as_of"},
        "get_executive_summary": {"date_from", "date_to", "company"},
    }
    if name in allowed:
        unknown = set(args.keys()) - allowed[name]
        if unknown:
            args = {k: v for k, v in args.items() if k in allowed[name]}

    # Coerce numeric args. Llama (via Groq) sometimes emits "limit": "10"
    # as a string; Groq's server-side schema validation then rejects the
    # whole call with `tool_use_failed`. Coercing here makes the schema
    # mismatch survivable on whatever path actually executes the tool.
    _int_args = {"limit", "threshold", "days", "months"}
    for key in list(args.keys()):
        if key in _int_args and isinstance(args[key], str):
            try:
                args[key] = int(args[key].strip())
            except (ValueError, AttributeError):
                args.pop(key, None)

    # Cache key includes the user's API key so cache is per-user (preserves
    # ERPNext permission scoping — Ahmed's cache won't leak to Sara).
    cache_key = tool_cache.make_key(
        "tool", name, args,
        erpnext_client.base_url,
        (erpnext_client.api_key or "")[:8],  # short prefix avoids storing full key
    )
    cached = await tool_cache.get(cache_key)
    if cached is not None:
        from loguru import logger
        logger.info(f"[cache hit] {name} args={args}")
        return cached

    result: Any
    try:
        if name == "get_unpaid_invoices":
            result = await erpnext_client.get_unpaid_invoices(**args)
        elif name == "get_low_stock_items":
            result = await erpnext_client.get_low_stock_items(**args)
        elif name == "get_sales_summary":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_sales_summary يحتاج date_from و date_to"}
            result = await erpnext_client.get_sales_summary(**args)
        elif name == "get_top_customers":
            result = await erpnext_client.get_top_customers(**args)
        elif name == "get_top_suppliers":
            result = await erpnext_client.get_top_suppliers(**args)
        elif name == "get_open_sales_orders":
            result = await erpnext_client.get_open_sales_orders(**args)
        elif name == "get_recent_purchase_invoices":
            result = await erpnext_client.get_recent_purchase_invoices(**args)
        elif name == "get_list":
            if not args.get("doctype"):
                return {"error": "get_list يحتاج اسم doctype"}
            result = await erpnext_client.get_list(**args)
        elif name == "get_profit_loss_report":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_profit_loss_report يحتاج date_from و date_to"}
            result = await erpnext_client.get_profit_loss_report(**args)
        elif name == "get_balance_sheet":
            if "as_of" not in args:
                return {"error": "get_balance_sheet يحتاج as_of"}
            result = await erpnext_client.get_balance_sheet(**args)
        elif name == "get_accounts_receivable":
            result = await erpnext_client.get_accounts_receivable(**args)
        elif name == "get_general_ledger":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_general_ledger يحتاج date_from و date_to"}
            result = await erpnext_client.get_general_ledger(**args)
        elif name == "get_trial_balance":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_trial_balance يحتاج date_from و date_to"}
            result = await erpnext_client.get_trial_balance(**args)
        elif name == "get_cash_flow_report":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_cash_flow_report يحتاج date_from و date_to"}
            result = await erpnext_client.get_cash_flow_report(**args)
        elif name == "get_customer_profitability":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_customer_profitability يحتاج date_from و date_to"}
            result = await erpnext_client.get_customer_profitability(**args)
        elif name == "get_item_profitability":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_item_profitability يحتاج date_from و date_to"}
            result = await erpnext_client.get_item_profitability(**args)
        elif name == "get_gross_profit":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_gross_profit يحتاج date_from و date_to"}
            result = await erpnext_client.get_gross_profit(**args)
        elif name == "get_monthly_sales_trend":
            result = await erpnext_client.get_monthly_sales_trend(**args)
        elif name == "get_expense_breakdown":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_expense_breakdown يحتاج date_from و date_to"}
            result = await erpnext_client.get_expense_breakdown(**args)
        elif name == "get_cash_position":
            result = await erpnext_client.get_cash_position(**args)
        elif name == "get_payables_summary":
            result = await erpnext_client.get_payables_summary(**args)
        elif name == "get_executive_summary":
            if "date_from" not in args or "date_to" not in args:
                return {"error": "get_executive_summary يحتاج date_from و date_to"}
            result = await erpnext_client.get_executive_summary(**args)
        else:
            return {"error": f"الأداة غير معروفة: {name}"}
    except TypeError as e:
        return {"error": f"خطأ في معطيات الأداة: {e}"}
    except Exception as e:
        return {"error": str(e)}

    # Don't cache errors. Cache successful results for 5 minutes — most ERP
    # data (totals, lists, summaries) is stable enough at that timescale.
    if not (isinstance(result, dict) and "error" in result):
        await tool_cache.set(cache_key, result, ttl=300.0)
    return result
