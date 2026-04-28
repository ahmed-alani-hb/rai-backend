"""Dashboard spec models — AI generates a list of cards, Flutter renders them."""
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class DashboardCard(BaseModel):
    id: str
    title_ar: str
    subtitle_ar: Optional[str] = None
    icon: str = "dashboard"
    color: str = "primary"
    card_type: Literal["kpi", "list", "table", "chart_bar", "chart_pie"] = "kpi"
    span: Literal[1, 2] = 1
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)
    value: Optional[str] = None
    delta: Optional[str] = None
    rows: Optional[list[dict[str, Any]]] = None
    drilldown_prompt_ar: Optional[str] = None


class DashboardSpec(BaseModel):
    title_ar: str = "لوحة التحكم"
    greeting_ar: str = "أهلاً بك"
    cards: list[DashboardCard] = Field(default_factory=list)
    generated_for_roles: list[str] = Field(default_factory=list)
    ai_provider: str = "claude"


class RefreshCardRequest(BaseModel):
    card: DashboardCard
    # Language for the localized subtitle/value strings refresh_card writes
    # back into the card. Falls back to Arabic for backward compatibility.
    lang: str = "ar"


class ModifyDashboardRequest(BaseModel):
    """User asks AI to modify the current dashboard."""
    current_spec: DashboardSpec
    instruction_ar: str = Field(
        ...,
        description="What to change in Arabic, e.g. 'أضف بطاقة لأكبر 5 موردين' or 'احذف بطاقة الفواتير'",
    )
