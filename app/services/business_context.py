"""Business context loader.

On first chat per session, fetches a snapshot of the user's company structure
so the AI doesn't waste tool calls on discovery. The snapshot lists:
- Company name, default currency, fiscal year
- Territories (Mosul, Erbil, ...) so AI knows which exist
- Top customers and suppliers by name
- Customer / item / supplier groups

The snapshot is cached server-side for 1 hour per (user, erp_url). Refreshing
doesn't change unless the customer adds new entities — 1h is conservative.
"""
import asyncio
from typing import Any

from loguru import logger

from app.services.cache import tool_cache
from app.services.erpnext_client import ERPNextClient

CONTEXT_TTL = 3600.0  # 1 hour


async def get_business_context(erp: ERPNextClient) -> dict[str, Any]:
    """Returns a compact dict describing the user's company.
    Result is cached server-side per (user, erp_url) for 1 hour.
    """
    cache_key = tool_cache.make_key(
        "business_context",
        erp.base_url,
        (erp.api_key or "")[:8],
    )
    cached = await tool_cache.get(cache_key)
    if cached is not None:
        return cached

    context = await _fetch_context(erp)
    await tool_cache.set(cache_key, context, ttl=CONTEXT_TTL)
    return context


async def _fetch_context(erp: ERPNextClient) -> dict[str, Any]:
    """Run all probes in parallel. Failures on individual probes are silent —
    we want to never block the chat on a slow ERPNext."""

    async def safe(coro, default):
        try:
            return await coro
        except Exception as e:
            logger.warning(f"context probe failed: {e}")
            return default

    company_task = safe(_fetch_company(erp), None)
    territories_task = safe(
        erp.get_list("Territory", fields=["name", "territory_name"], limit=30),
        [],
    )
    customer_groups_task = safe(
        erp.get_list("Customer Group", fields=["name"], limit=20),
        [],
    )
    item_groups_task = safe(
        erp.get_list("Item Group", fields=["name"], limit=30),
        [],
    )
    top_customers_task = safe(erp.get_top_customers(limit=15), [])
    top_suppliers_task = safe(erp.get_top_suppliers(limit=10), [])

    (
        company,
        territories,
        customer_groups,
        item_groups,
        top_customers,
        top_suppliers,
    ) = await asyncio.gather(
        company_task,
        territories_task,
        customer_groups_task,
        item_groups_task,
        top_customers_task,
        top_suppliers_task,
    )

    return {
        "company": company,
        "territories": [t.get("territory_name") or t.get("name") for t in (territories or [])],
        "customer_groups": [g.get("name") for g in (customer_groups or [])],
        "item_groups": [g.get("name") for g in (item_groups or [])],
        "top_customers": [
            {
                "name": c.get("customer_name") or c.get("customer") or c.get("name"),
                "territory": c.get("territory"),
                # New field name (post 2026-04 refactor) with backward fallback.
                "total_sales": c.get("total_sales_base_ccy") or c.get("total_sales"),
            }
            for c in (top_customers or [])
        ],
        "top_suppliers": [
            s.get("supplier_name") or s.get("name") for s in (top_suppliers or [])
        ],
    }


async def _fetch_company(erp: ERPNextClient) -> dict[str, Any] | None:
    """Try to get the default Company doc."""
    companies = await erp.get_list(
        "Company",
        fields=["name", "company_name", "default_currency", "country"],
        limit=1,
    )
    return companies[0] if companies else None


def format_for_prompt(ctx: dict[str, Any]) -> str:
    """Render the context as Arabic prompt-friendly bullets.
    Keeps the prompt small — only includes non-empty sections.
    """
    if not ctx:
        return ""
    lines = ["## السياق التجاري للشركة (مرجع — لا تكرّر استدعاء أدوات لتأكيد هذه القيم)"]

    company = ctx.get("company") or {}
    if company:
        lines.append(
            f"- **الشركة**: {company.get('company_name') or company.get('name')}"
            f" | العملة: {company.get('default_currency') or 'غير محدّد'}"
            + (f" | البلد: {company['country']}" if company.get('country') else "")
        )

    territories = ctx.get("territories") or []
    if territories:
        lines.append(f"- **المناطق المتاحة**: {', '.join(territories[:30])}")

    customer_groups = ctx.get("customer_groups") or []
    if customer_groups:
        lines.append(f"- **مجموعات العملاء**: {', '.join(customer_groups[:15])}")

    top_customers = ctx.get("top_customers") or []
    if top_customers:
        names = [c["name"] for c in top_customers if c.get("name")][:10]
        lines.append(f"- **أكبر العملاء**: {', '.join(names)}")

    top_suppliers = ctx.get("top_suppliers") or []
    if top_suppliers:
        lines.append(f"- **الموردون**: {', '.join(top_suppliers[:10])}")

    item_groups = ctx.get("item_groups") or []
    if item_groups:
        lines.append(f"- **مجموعات الأصناف**: {', '.join(item_groups[:15])}")

    return "\n".join(lines)
