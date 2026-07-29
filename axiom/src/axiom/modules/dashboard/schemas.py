from __future__ import annotations

from pydantic import BaseModel, Field

from axiom.modules.audit.schemas import AuditEvent


class TokenUsageSummary(BaseModel):
    input: int = 0
    output: int = 0
    total: int = 0


class TaskTokenUsage(TokenUsageSummary):
    task_id: str
    title: str


class DashboardSummary(BaseModel):
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    vulnerability_severity_counts: dict[str, int] = Field(default_factory=dict)
    active_intent_count: int = 0
    pending_intent_count: int = 0
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_by_task: list[TaskTokenUsage] = Field(default_factory=list)
    recent_events: list[AuditEvent] = Field(default_factory=list)
