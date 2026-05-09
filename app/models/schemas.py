"""Pydantic schemas — data exchanged between Flutter and the backend."""
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Two modes:
      1. RAI account login (the normal path):
           email + password               — RAI account on Honey Bird
           erp_url + erp_username + erp_password — sent from device
                                            secure storage. Used to
                                            regenerate ERP keys for
                                            this session, not stored.
      2. Direct ERP login (legacy / dev / escape hatch):
           erp_url + username + password
    """
    # RAI account auth
    email: Optional[str] = None
    password: str  # In RAI mode = RAI password. In direct mode = ERP password.

    # ERP creds — required in RAI mode (sent from flutter_secure_storage),
    # equivalent to the legacy `username` field for direct mode.
    erp_url: Optional[str] = None
    erp_username: Optional[str] = None  # RAI mode
    erp_password: Optional[str] = None  # RAI mode
    username: Optional[str] = None       # Legacy direct mode

    erp_type: Literal["erpnext", "odoo"] = "erpnext"

    # Subscription enforcement plumbing — optional today (logged-only),
    # graduates to enforced 403s when paid tiers ship.
    device_id: Optional[str] = None
    device_label: Optional[str] = None  # e.g. "iPhone 15 Pro" — display only


class LoginWithKeysRequest(BaseModel):
    erp_url: str
    api_key: str
    api_secret: str
    erp_type: Literal["erpnext", "odoo"] = "erpnext"


class SignupRequest(BaseModel):
    """One-shot signup. ERP credentials are validated against the user's
    ERP, used to seed an api_key/api_secret pair for the session, then
    discarded server-side — the Flutter app retains them in encrypted
    secure storage on the device only."""
    email: str
    password: str = Field(..., min_length=8)
    full_name: str = Field(..., min_length=2)
    phone: Optional[str] = None

    # The user's own ERPNext that RAI will query on their behalf.
    erp_url: str
    erp_username: str
    erp_password: str
    erp_type: Literal["erpnext", "odoo"] = "erpnext"

    # Same plumbing as LoginRequest — see comment there.
    device_id: Optional[str] = None
    device_label: Optional[str] = None


class SubscriptionStatus(BaseModel):
    """Current subscription state for a RAI account. Embedded in JWT
    so the gate check on protected routes is free."""
    status: Literal["trial", "active", "expired", "cancelled", "none"] = "none"
    days_remaining: int = 0
    end_date: Optional[str] = None  # YYYY-MM-DD
    plan_name: Optional[str] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_full_name: Optional[str] = None
    user_roles: list[str] = Field(default_factory=list)
    erp_type: str
    subscription: SubscriptionStatus = Field(default_factory=SubscriptionStatus)


class SignupResponse(BaseModel):
    """Returned by /auth/signup. Same shape as login response so the
    Flutter side can route to dashboard immediately after signup
    instead of forcing a second round-trip."""
    access_token: str
    token_type: str = "bearer"
    user_full_name: str
    subscription: SubscriptionStatus


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    ai_provider: Optional[Literal["openai", "claude", "gemini", "groq"]] = None
    stream: bool = False
    # Language the user expects the AI to reply in. Maps to the system
    # prompt variant. Falls back to Arabic if absent so existing clients
    # keep working unchanged.
    lang: Literal["ar", "ckb", "en"] = "ar"
    # Free-text facts the user wants the AI to remember across sessions
    # (max ~20 entries, ~280 chars each — capped client-side). Empty list
    # is the default; old clients sending no value behave unchanged.
    user_memory: list[str] = Field(default_factory=list)


class ToolCallTrace(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    result_summary: str


class ChatResponse(BaseModel):
    message: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    table_data: Optional[list[dict[str, Any]]] = None
    chart_data: Optional[dict[str, Any]] = None
    # Which provider actually answered. May differ from the requested one
    # if a fallback fired (e.g. Gemini rate-limited → Claude responded).
    provider_used: Optional[str] = None


class AppSettings(BaseModel):
    ai_provider: Literal["openai", "claude", "gemini", "groq"] = "groq"
    language: Literal["ar", "ckb", "en"] = "ar"
