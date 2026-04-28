"""Health check + config dump + report-debug."""
from typing import Annotated, Any, Optional
from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import get_settings
from app.services.erpnext_client import ERPNextClient

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/debug/companies")
async def list_companies(user: Annotated[CurrentUser, Depends(get_current_user)]):
    """Lists all companies in the ERP with their default currencies.
    Use this to verify the exact company names to pass to reports.
    """
    erp = ERPNextClient(user.erp_url, user.api_key, user.api_secret)
    return await erp.get_list(
        "Company",
        fields=["name", "company_name", "default_currency", "country", "abbr"],
        limit=20,
    )


@router.get("/debug/run_report")
async def debug_run_report(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    report_name: str = "Profit and Loss Statement",
    company: Optional[str] = None,
    from_date: str = "2026-01-01",
    to_date: str = "2026-12-31",
    periodicity: str = "Monthly",
):
    """Calls a Frappe Query Report directly and returns the RAW response.
    Wrapped in try/except so we always see the actual error.
    """
    import httpx
    import json as _json
    erp = ERPNextClient(user.erp_url, user.api_key, user.api_secret)

    if not company:
        companies = await erp.get_list("Company", fields=["name"], limit=1)
        company = companies[0]["name"] if companies else None

    # ERPNext v15+ Script Reports use period_start_date / period_end_date.
    # We send both so older versions still work.
    filters: dict[str, Any] = {
        "company": company,
        "period_start_date": from_date,
        "period_end_date": to_date,
        "from_date": from_date,
        "to_date": to_date,
        "periodicity": periodicity,
        "filter_based_on": "Date Range",
        "include_default_book_entries": 1,
        "accumulated_values": 0,
    }

    url = f"{erp.base_url}/api/method/frappe.desk.query_report.run"
    body = _json.dumps({
        "report_name": report_name,
        "filters": filters,
        "ignore_prepared_report": True,
    })

    async with erp._client() as client:
        try:
            # GET with a JSON body — yes this is Frappe v16's quirk.
            resp = await client.request(
                "GET", url,
                content=body,
                headers={
                    **erp._auth_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except Exception as e:
            return {
                "ok": False,
                "stage": "request",
                "error": f"{type(e).__name__}: {e}",
                "url": url,
                "filters": filters,
            }

        body_text = resp.text
        try:
            body_json = resp.json()
        except Exception:
            body_json = None

        result = (body_json or {}).get("message", {}) or {}

        return {
            "ok": resp.status_code == 200,
            "http_status": resp.status_code,
            "report_name": report_name,
            "filters_used": filters,
            "row_count": len(result.get("result", []) or []),
            "column_count": len(result.get("columns", []) or []),
            "columns": result.get("columns"),
            "first_30_rows": (result.get("result") or [])[:30],
            "report_summary": result.get("report_summary"),
            # If the request failed, this gives us Frappe's actual error message
            "raw_response_excerpt": body_text[:2000] if resp.status_code != 200 else None,
            "response_headers": dict(resp.headers),
        }


@router.get("/debug/list_reports")
async def list_reports(user: Annotated[CurrentUser, Depends(get_current_user)]):
    """Lists all available reports — useful to confirm exact report names.
    """
    erp = ERPNextClient(user.erp_url, user.api_key, user.api_secret)
    return await erp.get_list(
        "Report",
        fields=["name", "report_type", "ref_doctype", "module"],
        filters=[["report_type", "in", ["Script Report", "Query Report", "Custom Report"]]],
        limit=200,
    )


@router.post("/debug/raw_report")
async def debug_raw_report(payload: dict[str, Any]):
    """Calls a Frappe Query Report against ANY Frappe site using direct
    credentials — useful for diagnosing whether issues are code-side or
    server-side. NO JWT required (this endpoint is unauthenticated).

    Body shape:
    {
      "erp_url": "https://syl-alshimal.f.frappe.cloud",
      "api_key": "49e01952ff18dbc",
      "api_secret": "96b02e35cbc1936",
      "report_name": "Activation Summary 2",
      "filters": {"from_date": "2026-01-01", "to_date": "2026-01-22", "group_by": "Customer", "date_field": "Sales Date"}
    }
    """
    import json as _json

    erp_url = payload.get("erp_url")
    api_key = payload.get("api_key")
    api_secret = payload.get("api_secret")
    report_name = payload.get("report_name")
    filters = payload.get("filters") or {}

    if not all([erp_url, api_key, api_secret, report_name]):
        return {
            "ok": False,
            "error": "missing required: erp_url, api_key, api_secret, report_name",
        }

    erp = ERPNextClient(erp_url, api_key, api_secret)
    url = f"{erp.base_url}/api/method/frappe.desk.query_report.run"
    body = _json.dumps({
        "report_name": report_name,
        "filters": filters,
        "ignore_prepared_report": True,
    })

    async with erp._client() as client:
        try:
            resp = await client.request(
                "GET", url,
                content=body,
                headers={
                    **erp._auth_headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except Exception as e:
            return {"ok": False, "stage": "request", "error": f"{type(e).__name__}: {e}"}

        body_text = resp.text
        try:
            body_json = resp.json()
        except Exception:
            body_json = None
        result = (body_json or {}).get("message", {}) or {}

        return {
            "ok": resp.status_code == 200,
            "http_status": resp.status_code,
            "report_name": report_name,
            "filters_used": filters,
            "row_count": len(result.get("result", []) or []),
            "column_count": len(result.get("columns", []) or []),
            "columns": result.get("columns"),
            "first_30_rows": (result.get("result") or [])[:30],
            "report_summary": result.get("report_summary"),
            "raw_response_excerpt": body_text[:2000] if resp.status_code != 200 else None,
            "set_cookie": resp.headers.get("set-cookie", "")[:300],
        }


@router.get("/config")
def config_dump():
    """Returns the actually-loaded config values (secrets redacted).
    Useful for debugging "did my .env change take effect?" questions.
    """
    s = get_settings()
    def mask(v: str, keep: int = 4) -> str:
        if not v:
            return "(empty)"
        return f"{v[:keep]}...({len(v)} chars)"
    return {
        "app_env": s.app_env,
        "default_ai_provider": s.default_ai_provider,
        "openai": {
            "model": s.openai_model,
            "key": mask(s.openai_api_key),
        },
        "anthropic": {
            "model": s.anthropic_model,
            "key": mask(s.anthropic_api_key),
        },
        "gemini": {
            "model": s.gemini_model,
            "key": mask(s.gemini_api_key),
        },
        "groq": {
            "whisper_model": s.groq_whisper_model,
            "classifier_model": s.groq_classifier_model,
            "chat_model": s.groq_chat_model,
            "key": mask(s.groq_api_key),
        },
    }


@router.get("/config")
def config_dump():
    """Returns the actually-loaded config values (secrets redacted).
    Useful for debugging "did my .env change take effect?" questions.
    """
    s = get_settings()
    def mask(v: str, keep: int = 4) -> str:
        if not v:
            return "(empty)"
        return f"{v[:keep]}...({len(v)} chars)"
    return {
        "app_env": s.app_env,
        "default_ai_provider": s.default_ai_provider,
        "openai": {
            "model": s.openai_model,
            "key": mask(s.openai_api_key),
        },
        "anthropic": {
            "model": s.anthropic_model,
            "key": mask(s.anthropic_api_key),
        },
        "gemini": {
            "model": s.gemini_model,
            "key": mask(s.gemini_api_key),
        },
        "groq": {
            "whisper_model": s.groq_whisper_model,
            "classifier_model": s.groq_classifier_model,
            "key": mask(s.groq_api_key),
        },
    }
