"""App configuration — reads from .env"""
from typing import List, Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "RAI"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    secret_key: str = "change-me"
    allowed_origins: str = "http://localhost:3000"

    erpnext_base_url: str = ""
    erpnext_api_key: str = ""
    erpnext_api_secret: str = ""

    odoo_url: str = ""
    odoo_db: str = ""
    odoo_username: str = ""
    odoo_password: str = ""

    default_ai_provider: Literal["openai", "claude", "gemini", "groq"] = "groq"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"  # 1,500 RPD free vs 20 on 2.5-flash

    # Groq for Arabic speech-to-text (Whisper). Free tier covers heavy testing.
    groq_api_key: str = ""
    groq_whisper_model: str = "whisper-large-v3"
    groq_classifier_model: str = "llama-3.3-70b-versatile"
    # Groq as a chat provider too — Llama 3.3 70B with tool calling.
    # Free tier is generous (~14,400 RPD), great for development and small users.
    groq_chat_model: str = "llama-3.3-70b-versatile"

    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    redis_url: str = "redis://localhost:6379/0"

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    # Defensive whitespace stripping for secrets. Cloud Secret Manager + a
    # PowerShell `echo` pipe on Windows produces values with trailing CRLF,
    # which becomes a `Bearer <key>\r\n` header value that httpx rejects with
    # LocalProtocolError. Strip any whitespace, BOM, and zero-width chars
    # so a malformed secret is harmless going forward.
    @field_validator(
        "secret_key",
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "groq_api_key",
        "erpnext_api_key",
        "erpnext_api_secret",
        mode="before",
    )
    @classmethod
    def _strip_secret(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            # Strip whitespace, BOM, and zero-width spaces from both ends.
            return v.strip().strip("﻿​‌‍")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
