"""Auth endpoints."""
from fastapi import APIRouter, HTTPException, status
from loguru import logger

from app.core.security import create_access_token
from app.models.schemas import LoginRequest, LoginResponse, LoginWithKeysRequest
from app.services.erpnext_client import ERPNextClient, ERPNextAuthError

router = APIRouter()


@router.post("/login_with_keys", response_model=LoginResponse)
async def login_with_keys(req: LoginWithKeysRequest):
    """Login with pre-generated API key + secret (recommended for production)."""
    if req.erp_type != "erpnext":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="حالياً ندعم ERPNext فقط — Odoo قريباً",
        )

    client = ERPNextClient(
        base_url=req.erp_url,
        api_key=req.api_key,
        api_secret=req.api_secret,
    )

    try:
        info = await client.whoami()
    except ERPNextAuthError as e:
        logger.warning(f"key auth failed: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except Exception as e:
        logger.exception("key auth error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"تعذّر الاتصال بـ ERPNext: {e}",
        )

    token = create_access_token(
        subject=info["username"],
        extra_claims={
            "erp_url": req.erp_url,
            "erp_type": req.erp_type,
            "api_key": req.api_key,
            "api_secret": req.api_secret,
            "roles": info["roles"],
            "full_name": info["full_name"],
        },
    )
    return LoginResponse(
        access_token=token,
        user_full_name=info["full_name"],
        user_roles=info["roles"],
        erp_type=req.erp_type,
    )


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Login with username + password (auto-generates API keys)."""
    if req.erp_type != "erpnext":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="حالياً ندعم ERPNext فقط — Odoo قريباً",
        )

    client = ERPNextClient(base_url=req.erp_url)
    try:
        auth_data = await client.login_and_generate_keys(req.username, req.password)
    except ERPNextAuthError as e:
        logger.warning(f"login failed for {req.username}: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"فشل تسجيل الدخول: {e}",
        )
    except Exception as e:
        logger.exception("unexpected login error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"تعذّر الاتصال بـ ERPNext: {e}",
        )

    token = create_access_token(
        subject=req.username,
        extra_claims={
            "erp_url": req.erp_url,
            "erp_type": req.erp_type,
            "api_key": auth_data["api_key"],
            "api_secret": auth_data["api_secret"],
            "roles": auth_data.get("roles", []),
            "full_name": auth_data.get("full_name", req.username),
        },
    )

    return LoginResponse(
        access_token=token,
        user_full_name=auth_data.get("full_name", req.username),
        user_roles=auth_data.get("roles", []),
        erp_type=req.erp_type,
    )
