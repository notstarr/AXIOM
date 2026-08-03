from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


TokenRange = Literal["7d", "30d", "3m", "6m", "1y", "all"]
UsageKind = Literal["assistant", "compaction", "branch_summary", "tool_result", "legacy_audit"]
UsageSource = Literal["live", "backfill", "legacy"]


class TokenUsageRecordInput(BaseModel):
    session_id: str = ""
    session_file: str
    session_entry_id: str
    usage_kind: UsageKind
    occurred_at: str
    provider: str = ""
    model: str = ""
    requested_model: str = ""
    input_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    reported_total_tokens: int | None = Field(default=None, ge=0)

    @field_validator(
        "session_id",
        "session_entry_id",
        "occurred_at",
        "provider",
        "model",
        "requested_model",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("session_file")
    @classmethod
    def validate_session_file(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("session_file must be a safe relative path")
        return normalized

    @field_validator("session_entry_id")
    @classmethod
    def require_entry_id(cls, value: str) -> str:
        if not value:
            raise ValueError("session_entry_id must not be empty")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timestamp(cls, value: str) -> str:
        if not value:
            raise ValueError("occurred_at must not be empty")
        return value


class TokenUsageSourceInput(BaseModel):
    session_file: str
    session_id: str = ""
    status: Literal["complete", "incomplete"]
    error: str = Field(default="", max_length=1_000)

    @field_validator("session_file")
    @classmethod
    def validate_session_file(cls, value: str) -> str:
        return TokenUsageRecordInput.validate_session_file(value)

    @field_validator("session_id", "error")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class TokenUsageBatchCommand(BaseModel):
    lease_id: str
    records: list[TokenUsageRecordInput] = Field(default_factory=list, max_length=500)
    sources: list[TokenUsageSourceInput] = Field(default_factory=list, max_length=100)

    @field_validator("lease_id")
    @classmethod
    def require_lease(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("lease_id must not be empty")
        return normalized


class TokenUsageBatchResult(BaseModel):
    inserted: int = 0
    duplicate: int = 0
    sources_updated: int = 0


class TokenUsageSummary(BaseModel):
    input: int = 0
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    total: int = 0
    calls: int = 0
    cache_hit_rate: float | None = None
    complete: bool = True
    incomplete_sources: int = 0


class TaskTokenUsage(TokenUsageSummary):
    task_id: str
    title: str
    status: str = ""


class TokenUsageSeriesPoint(BaseModel):
    bucket: str
    input: int = 0
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    total: int = 0


class TokenUsageModel(BaseModel):
    key: str
    provider: str
    model: str
    calls: int = 0


class TokenUsageDashboard(BaseModel):
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_by_task: list[TaskTokenUsage] = Field(default_factory=list)
    token_series: list[TokenUsageSeriesPoint] = Field(default_factory=list)
    available_models: list[TokenUsageModel] = Field(default_factory=list)
    token_range: TokenRange = "all"
    token_model: str = "all"
