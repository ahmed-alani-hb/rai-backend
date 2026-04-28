"""Dashboard endpoints."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from loguru import logger

from app.api.dependencies import CurrentUser, get_current_user
from app.models.dashboard import (
    DashboardCard, DashboardSpec, ModifyDashboardRequest, RefreshCardRequest,
)
from app.services.dashboard_ai import (
    generate_dashboard, modify_dashboard, refresh_card,
)
from app.services.erpnext_client import ERPNextClient

router = APIRouter()


class ModifyDashboardResponse(BaseModel):
    spec: DashboardSpec
    modified: bool
    message_ar: str


class GenerateDashboardRequest(BaseModel):
    """Optional body for /generate. Old clients can keep POSTing without
    a body — FastAPI treats it as defaults."""
    lang: str = "ar"


@router.post("/generate", response_model=DashboardSpec)
async def generate(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    req: GenerateDashboardRequest | None = None,
):
    erp = ERPNextClient(
        base_url=user.erp_url,
        api_key=user.api_key,
        api_secret=user.api_secret,
    )
    spec = await generate_dashboard(
        user_full_name=user.full_name,
        roles=user.roles,
        erp=erp,
        lang=(req.lang if req else "ar"),
    )
    logger.info(f"/generate returned {len(spec.cards)} cards (provider={spec.ai_provider})")
    return spec


@router.post("/refresh_card", response_model=DashboardCard)
async def refresh_card_endpoint(
    req: RefreshCardRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
):
    erp = ERPNextClient(
        base_url=user.erp_url,
        api_key=user.api_key,
        api_secret=user.api_secret,
    )
    return await refresh_card(req.card, erp, lang=req.lang)


@router.post("/modify", response_model=ModifyDashboardResponse)
async def modify_dashboard_endpoint(
    req: ModifyDashboardRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
):
    """Apply a user instruction to the current dashboard layout.

    Example: 'أضف بطاقة لأكبر 5 موردين' → returns spec with one new card.
    The Flutter app then re-runs /refresh_card on any new/changed cards.
    """
    if not req.instruction_ar.strip():
        raise HTTPException(status_code=400, detail="instruction_ar is required")

    new_spec, modified = await modify_dashboard(
        current_spec=req.current_spec,
        instruction_ar=req.instruction_ar.strip(),
        user_full_name=user.full_name,
        roles=user.roles,
    )

    if modified:
        msg = f"تم تطبيق التعديل ({len(new_spec.cards)} بطاقة الآن)."
    else:
        msg = "لم أتمكّن من فهم الطلب. حاول صياغته بطريقة أخرى."

    logger.info(
        f"/modify instruction='{req.instruction_ar[:50]}' "
        f"modified={modified} cards={len(new_spec.cards)}"
    )
    return ModifyDashboardResponse(spec=new_spec, modified=modified, message_ar=msg)
