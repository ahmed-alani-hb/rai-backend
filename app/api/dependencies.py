"""Shared dependencies — extract current user from JWT.

Uses HTTPBearer so Swagger's Authorize dialog asks for one token field.
"""
from typing import Annotated, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    def __init__(self, payload: dict):
        self.username: str = payload["sub"]
        self.erp_url: str = payload.get("erp_url", "")
        self.erp_type: str = payload.get("erp_type", "erpnext")
        self.api_key: str = payload.get("api_key", "")
        self.api_secret: str = payload.get("api_secret", "")
        self.roles: list[str] = payload.get("roles", [])
        self.full_name: str = payload.get("full_name", "")
        # Subscription state from JWT — populated by /auth/login &
        # /auth/signup. None / "active" / "trial" / "expired" / "cancelled".
        self.sub_status: str = payload.get("sub_status", "active")
        self.sub_days_remaining: int = payload.get("sub_days_remaining", 0)


def get_current_user(
    creds: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)],
) -> CurrentUser:
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="غير مصرح — لم يتم توفير token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(creds.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token غير صالح أو منتهي الصلاحية",
        )
    return CurrentUser(payload)


def require_active_subscription(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Drop-in replacement for `get_current_user` on routes that need
    a paying / trialing user. Returns 402 Payment Required when the
    JWT carries an expired/cancelled/none subscription so Flutter can
    show the paywall instead of a generic error.

    Active states: "active", "trial".
    Blocked states: "expired", "cancelled", "none".
    """
    if user.sub_status not in ("active", "trial"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "subscription_required",
                "status": user.sub_status,
                "message": "اشتراكك انتهى — يرجى التواصل مع مدير حسابك للتجديد.",
            },
        )
    return user
