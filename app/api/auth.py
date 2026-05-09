"""Auth endpoints — signup, login (RAI account or direct ERP), key login.

Architecture note: ERP credentials are NEVER persisted server-side.
The Flutter app stores them in flutter_secure_storage and sends them
on signup AND every login. The backend uses them once per session to
regenerate ERP API keys, which then ride along in the JWT for the
session's duration. After the session expires the backend has no way
to call the user's ERP without the app sending the creds again.
"""
from fastapi import APIRouter, HTTPException, status
from loguru import logger

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.schemas import (
    LoginRequest, LoginResponse, LoginWithKeysRequest,
    SignupRequest, SignupResponse, SubscriptionStatus,
)
from app.services import honeybird_client as hb
from app.services.erpnext_client import ERPNextClient, ERPNextAuthError

router = APIRouter()
settings = get_settings()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _device_limit_exception(e: hb.DeviceLimitExceeded) -> HTTPException:
    """Friendly Arabic 403 for the device-cap case. Structured `code`
    so Flutter can pick the right copy without parsing strings."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "device_limit_exceeded",
            "current": e.current,
            "limit": e.limit,
            "message": (
                f"تم الوصول إلى الحد الأقصى للأجهزة ({e.current}/{e.limit}). "
                f"يرجى تسجيل الخروج من جهاز آخر أو ترقية الاشتراك."
            ),
        },
    )


async def _erp_login_for_session(
    erp_url: str, erp_username: str, erp_password: str,
) -> dict:
    """Wrap the ERP login call in matching exception types so callers
    don't need to know about ERPNextClient's internal exceptions."""
    erp_client = ERPNextClient(base_url=erp_url)
    try:
        return await erp_client.login_and_generate_keys(erp_username, erp_password)
    except ERPNextAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"فشل تسجيل الدخول إلى ERPNext الخاص بك: {e}",
        )
    except Exception as e:
        logger.exception("ERP auth probe error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"تعذّر الاتصال بـ ERPNext: {e}",
        )


# ────────────────────────────────────────────────────────────────────
# /signup — creates User + Customer + Subscription on Honey Bird,
# verifies the user's own ERP credentials work, returns a JWT so the
# Flutter app can drop them straight into the dashboard.
# ────────────────────────────────────────────────────────────────────


@router.post("/signup", response_model=SignupResponse)
async def signup(req: SignupRequest):
    if req.erp_type != "erpnext":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="حالياً ندعم ERPNext فقط — Odoo قريباً",
        )

    # 1. Block duplicate signups upfront with a friendly message.
    try:
        if await hb.user_exists(req.email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="هذا البريد مسجّل مسبقاً. جرّب تسجيل الدخول.",
            )
    except hb.HoneyBirdError as e:
        logger.exception(f"signup: user_exists check failed for {req.email}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"تعذّر الاتصال بسيرفر RAI: {e}",
        )

    # 2. Verify the user's ERP credentials BEFORE creating the Honey
    #    Bird records — fail fast if they typed wrong creds, no orphan
    #    User/Customer left behind.
    auth_data = await _erp_login_for_session(
        req.erp_url, req.erp_username, req.erp_password,
    )

    # 3. Create the records on Honey Bird. We DO NOT pass the ERP
    #    password — it stays on the user's device only.
    try:
        await hb.create_signup(
            email=req.email,
            password=req.password,
            full_name=req.full_name,
            phone=req.phone,
            trial_days=settings.rai_trial_days,
        )
    except hb.HoneyBirdError as e:
        logger.exception(f"signup: create_signup failed for {req.email}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"تعذّر إنشاء الحساب على RAI: {e}",
        )

    # 4. Register signup device + record url binding. Best-effort;
    #    failures here log but don't abort signup — the user's account
    #    is already created and we'd rather they get in than bounce.
    try:
        await hb.register_device(req.email, req.device_id or "", req.device_label)
    except hb.DeviceLimitExceeded as e:
        # On signup with one device this can't happen unless the plan
        # was misconfigured to max=0; treat as a server config issue.
        raise _device_limit_exception(e)
    except Exception:
        logger.exception("signup: register_device failed (non-fatal)")
    try:
        await hb.record_erp_url_binding(req.email, req.erp_url)
    except Exception:
        logger.exception("signup: record_erp_url_binding failed (non-fatal)")

    # 5. Subscription state — fresh signup is always trial.
    sub = SubscriptionStatus(
        status="trial",
        days_remaining=settings.rai_trial_days,
        end_date=None,  # client will refresh on next login
        plan_name="RAI Free Trial",
    )

    # 6. Issue the JWT — same shape downstream code expects, just with
    #    rai_email + subscription claims added.
    token = create_access_token(
        subject=req.email,
        extra_claims={
            "rai_email": req.email,
            "erp_url": req.erp_url,
            "erp_type": req.erp_type,
            "api_key": auth_data["api_key"],
            "api_secret": auth_data["api_secret"],
            "roles": auth_data.get("roles", []),
            "full_name": auth_data.get("full_name", req.full_name),
            "sub_status": sub.status,
            "sub_days_remaining": sub.days_remaining,
        },
    )
    logger.info(f"signup: success for {req.email} ({req.full_name})")
    return SignupResponse(
        access_token=token,
        user_full_name=auth_data.get("full_name", req.full_name),
        subscription=sub,
    )


# ────────────────────────────────────────────────────────────────────
# /login — RAI email+password OR direct ERP login (legacy / dev path)
# ────────────────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Two modes:
      1. RAI account: email + password (RAI account on Honey Bird) +
         erp_url + erp_username + erp_password (sent from Flutter
         secure storage). The backend verifies the RAI password,
         then uses the ERP creds to regenerate api_key/api_secret
         for this session. ERP creds are NOT persisted server-side.
      2. Direct ERP: when `email` is empty but `erp_url`+`username`+
         `password` are provided, skip Honey Bird and authenticate
         directly. Kept as a dev / escape-hatch path.
    """
    is_rai_login = bool(
        req.email and req.password
        and req.erp_url and req.erp_username and req.erp_password
    )
    is_direct_login = bool(
        not req.email
        and req.erp_url and req.username and req.password
    )

    if not (is_rai_login or is_direct_login):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "تنسيق طلب الدخول غير صحيح. لطلب دخول RAI، أرسل: "
                "email + password + erp_url + erp_username + erp_password."
            ),
        )

    if is_direct_login:
        return await _direct_erp_login(req)
    return await _rai_account_login(req)


async def _rai_account_login(req: LoginRequest) -> LoginResponse:
    # 1. Verify email/password against Honey Bird. Wrong password → 401.
    if not await hb.verify_password(req.email or "", req.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="البريد أو كلمة المرور غير صحيحة",
        )

    # 2. Use the ERP creds the app sent (from device storage) to log
    #    into the user's ERP and regenerate api_key/api_secret for
    #    this session. Type checker placation — these are non-empty
    #    by the is_rai_login check above.
    auth_data = await _erp_login_for_session(
        req.erp_url or "", req.erp_username or "", req.erp_password or "",
    )

    # 3. Look up subscription. Subscription failures don't block login —
    #    we return status="none" and let the gate decide.
    try:
        sub_dict = await hb.get_subscription_status(req.email or "")
    except hb.HoneyBirdError as e:
        logger.warning(f"subscription lookup failed for {req.email}: {e}")
        sub_dict = {"status": "none", "days_remaining": 0,
                    "end_date": None, "plan_name": None}
    sub = SubscriptionStatus(**sub_dict)

    # 4. Register device + record url binding. Device limit IS
    #    enforced (it's the only real "single user" gate); URL is
    #    logged-only for now.
    try:
        await hb.register_device(req.email or "", req.device_id or "", req.device_label)
    except hb.DeviceLimitExceeded as e:
        raise _device_limit_exception(e)
    except Exception:
        logger.exception("login: register_device failed (non-fatal)")
    try:
        await hb.record_erp_url_binding(req.email or "", req.erp_url or "")
    except Exception:
        logger.exception("login: record_erp_url_binding failed (non-fatal)")

    # 5. JWT with everything baked in. Subscription claims let the
    #    chat/dashboard middlewares reject expired sessions without
    #    a Honey Bird round-trip per request.
    token = create_access_token(
        subject=req.email or "",
        extra_claims={
            "rai_email": req.email,
            "erp_url": req.erp_url,
            "erp_type": "erpnext",
            "api_key": auth_data["api_key"],
            "api_secret": auth_data["api_secret"],
            "roles": auth_data.get("roles", []),
            "full_name": auth_data.get("full_name", req.email),
            "sub_status": sub.status,
            "sub_days_remaining": sub.days_remaining,
        },
    )
    return LoginResponse(
        access_token=token,
        user_full_name=auth_data.get("full_name", req.email),
        user_roles=auth_data.get("roles", []),
        erp_type="erpnext",
        subscription=sub,
    )


async def _direct_erp_login(req: LoginRequest) -> LoginResponse:
    """Legacy / dev path — bypasses Honey Bird entirely. Used by
    accounts that existed before signup shipped, and as an escape
    hatch when Honey Bird is down."""
    if req.erp_type != "erpnext":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="حالياً ندعم ERPNext فقط — Odoo قريباً",
        )
    auth_data = await _erp_login_for_session(
        req.erp_url or "", req.username or "", req.password,
    )

    token = create_access_token(
        subject=req.username or "",
        extra_claims={
            "erp_url": req.erp_url,
            "erp_type": req.erp_type,
            "api_key": auth_data["api_key"],
            "api_secret": auth_data["api_secret"],
            "roles": auth_data.get("roles", []),
            "full_name": auth_data.get("full_name", req.username),
            # Direct-login users skip the subscription gate entirely.
            "sub_status": "active",
            "sub_days_remaining": 99999,
        },
    )
    return LoginResponse(
        access_token=token,
        user_full_name=auth_data.get("full_name", req.username),
        user_roles=auth_data.get("roles", []),
        erp_type=req.erp_type,
        subscription=SubscriptionStatus(
            status="active", days_remaining=99999,
            plan_name="Developer / Direct"
        ),
    )


@router.post("/login_with_keys", response_model=LoginResponse)
async def login_with_keys(req: LoginWithKeysRequest):
    """Login with pre-generated API key + secret (recommended for
    automation scripts — bypasses every UX layer)."""
    if req.erp_type != "erpnext":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="حالياً ندعم ERPNext فقط — Odoo قريباً",
        )
    client = ERPNextClient(
        base_url=req.erp_url, api_key=req.api_key, api_secret=req.api_secret,
    )
    try:
        info = await client.whoami()
    except ERPNextAuthError as e:
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
            "erp_url": req.erp_url, "erp_type": req.erp_type,
            "api_key": req.api_key, "api_secret": req.api_secret,
            "roles": info["roles"], "full_name": info["full_name"],
            "sub_status": "active", "sub_days_remaining": 99999,
        },
    )
    return LoginResponse(
        access_token=token,
        user_full_name=info["full_name"],
        user_roles=info["roles"],
        erp_type=req.erp_type,
        subscription=SubscriptionStatus(
            status="active", days_remaining=99999, plan_name="Direct API"
        ),
    )
