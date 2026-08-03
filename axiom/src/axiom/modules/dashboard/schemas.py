from __future__ import annotations

from pydantic import BaseModel, Field

from axiom.modules.audit.schemas import AuditEvent
from axiom.modules.token_usage.schemas import (
    TaskTokenUsage,
    TokenRange,
    TokenUsageModel,
    TokenUsageSeriesPoint,
    TokenUsageSummary,
)


class DashboardSummary(BaseModel):
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    vulnerability_severity_counts: dict[str, int] = Field(default_factory=dict)
    active_intent_count: int = 0
    pending_intent_count: int = 0
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_by_task: list[TaskTokenUsage] = Field(default_factory=list)
    token_series: list[TokenUsageSeriesPoint] = Field(default_factory=list)
    available_models: list[TokenUsageModel] = Field(default_factory=list)
    token_range: TokenRange = "all"
    token_model: str = "all"
    recent_events: list[AuditEvent] = Field(default_factory=list)
