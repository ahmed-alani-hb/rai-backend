"""Per-customer ERP utility endpoints — list companies, bind one to
the subscription, etc. These are thin pass-throughs to the user's
own ERPNext using the api_key/api_secret embedded in their JWT.
"""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel

from app.api.dependencies import CurrentUser, require_active_subscription
from app.services import honeybird_client as hb
from app.services.erpnext_client import ERPNextClient, ERPNextAuthError

router = APIRouter()


class CompanyOption(BaseModel):
    name: str  # Company doctype's `name` (== company_name in most setups)
    company_name: str
    abbr: str
    parent_company: str | None = None
    is_group: bool = False


class CompaniesResponse(BaseModel):
    companies: list[CompanyOption]
    bound: str | None  # currently bound company on this Subscription / URL


class BindCompanyRequest(BaseModel):
    company: str


@router.get("/companies", response_model=CompaniesResponse)
async def list_companies(
    user: Annotated[CurrentUser, Depends(require_active_subscription)],
):
    """Return the Company records visible to this user in their own
    ERPNext, plus the company already bound for this user's current
    ERP URL on their Subscription (if any). Used by the Flutter app
    right after signup to drive the "which company should RAI work
    with?" picker."""
    if not (user.erp_url and user.api_key and user.api_secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JWT لا يحتوي على بيانات ERPNext — يرجى تسجيل الدخول من جديد.",
        )
    client = ERPNextClient(
        base_url=user.erp_url,
        api_key=user.api_key,
        api_secret=user.api_secret,
    )
    try:
        rows = await client.get_list(
            "Company",
            fields=["name", "company_name", "abbr", "parent_company", "is_group"],
            limit=50,
        )
    except ERPNextAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except Exception as e:
        logger.exception("list_companies: ERP error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"تعذّر قراءة الشركات من ERPNext: {e}",
        )

    bound: str | None = None
    if user.username:  # 'sub' claim — for RAI accounts it's the email
        try:
            sub = await hb._find_subscription(user.username)  # noqa: SLF001
            if sub:
                bindings = await hb._read_subscription_children(  # noqa: SLF001
                    sub["name"], "rai_bound_companies",
                )
                norm_url = hb._normalize_url(user.erp_url)  # noqa: SLF001
                for b in bindings:
                    if hb._normalize_url(b.get("erp_url") or "") == norm_url:  # noqa: SLF001
                        bound = b.get("company")
                        break
        except Exception:
            logger.exception("list_companies: subscription lookup failed (non-fatal)")

    return CompaniesResponse(
        companies=[CompanyOption(**r) for r in rows],
        bound=bound,
    )


@router.post("/bind_company")
async def bind_company(
    req: BindCompanyRequest,
    user: Annotated[CurrentUser, Depends(require_active_subscription)],
):
    """Record the user's chosen company on their Subscription's
    `rai_bound_companies` child table, paired with the current ERP URL
    from the JWT. First call on this URL inserts the row; later
    different-company calls log a warning but don't 403 yet — that
    graduates with paid tiers."""
    try:
        await hb.record_company_binding(
            email=user.username,
            erp_url=user.erp_url,
            company=req.company,
        )
    except Exception as e:
        logger.exception("bind_company: failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"تعذّر حفظ اختيار الشركة: {e}",
        )
    return {"ok": True, "company": req.company}
