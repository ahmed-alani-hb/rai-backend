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
