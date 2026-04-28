"""Pydantic schemas — data exchanged between Flutter and the backend."""
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    erp_url: str
    username: str
    password: str
    erp_type: Literal["erpnext", "odoo"] = "erpnext"


class LoginWithKeysRequest(BaseModel):
    erp_url: str
    api_key: str
    api_secret: str
    erp_type: Literal["erpnext", "odoo"] = "erpnext"


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_full_name: Optional[str] = None
    user_roles: list[str] = Field(default_factory=list)
    erp_type: str


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
