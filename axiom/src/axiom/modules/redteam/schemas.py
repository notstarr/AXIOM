from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


EngagementMode = Literal["vulnerability", "red_team"]
ResultType = Literal["permission", "data", "credential", "pivot"]
ChainStatus = Literal["open", "partial", "blocked", "completed", "abandoned"]
LEGACY_CHAIN_STATUS_MAP = {
    "planned": "open",
    "active": "partial",
    "achieved": "completed",
    "failed": "abandoned",
}
ACTIONABLE_CHAIN_STATUSES = ("open", "partial")
TERMINAL_CHAIN_STATUSES = ("blocked", "completed", "abandoned")
ResultStatus = Literal[
    "reported", "reviewing", "verified", "rejected", "duplicate", "needs-human"
]
# 护网表按件累计，不设总分上限。单条成果是单价 × 数量，字段宽度与目标分一致。
MAX_RESULT_POINTS = 10_000


class RedTeamPrincipal(BaseModel):
    id: str
    label: str = ""
    tenant: str = ""
    credential_id: str | None = None

    @field_validator("id", "label", "tenant")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("credential_id")
    @classmethod
    def normalize_credential_id(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


class RedTeamDataTarget(BaseModel):
    id: str
    max_records: int = Field(default=10, ge=1, le=10_000)
    max_bytes: int = Field(default=1_048_576, ge=1, le=100 * 1024 * 1024)
    allowed_fields: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("data target id must not be empty")
        return value

    @field_validator("allowed_fields")
    @classmethod
    def normalize_fields(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item.strip()))


class RedTeamObjective(BaseModel):
    initial_principal: RedTeamPrincipal
    target_capabilities: list[str] = Field(default_factory=list, max_length=200)
    target_assets: list[str] = Field(default_factory=list, max_length=200)
    data_targets: list[RedTeamDataTarget] = Field(default_factory=list, max_length=100)
    target_points: int = Field(default=100, ge=1, le=10_000)

    @field_validator("target_capabilities", "target_assets")
    @classmethod
    def normalize_targets(cls, values: list[str]) -> list[str]:
        values = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        return values

    @model_validator(mode="after")
    def require_target(self) -> "RedTeamObjective":
        # 空目标列表是合法语义：不设结构化上限，Reason 依任务目标自由
        # 规划，由成果审核与 Goal 校验兜底（用于"只切模式"的默认目标）。
        return self


class RedTeamChainCreate(BaseModel):
    title: str
    initial_principal: RedTeamPrincipal
    target_capabilities: list[str] = Field(default_factory=list, max_length=200)
    target_assets: list[str] = Field(default_factory=list, max_length=200)
    source_fact_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("title")
    @classmethod
    def require_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("chain title must not be empty")
        return value

    @field_validator("target_capabilities", "target_assets", "source_fact_ids")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item.strip()))


class RedTeamResultReport(BaseModel):
    # Optional for older callers. The service records the Intent's bound Chain.
    chain_id: str = ""
    result_type: ResultType
    title: str
    description: str
    principal: str
    tenant: str = ""
    capabilities_before: list[str] = Field(default_factory=list, max_length=200)
    capabilities_gained: list[str] = Field(default_factory=list, max_length=200)
    capabilities_after: list[str] = Field(default_factory=list, max_length=200)
    target_asset: str = ""
    data_target_id: str | None = None
    sample_count: int | None = Field(default=None, ge=0)
    sample_bytes: int | None = Field(default=None, ge=0)
    data_fields: list[str] = Field(default_factory=list, max_length=200)
    data_summary: str = ""
    credential_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    # 上报者按护网评分表自评（单价 × 数量）；落库以审核核定分为准
    points_declared: int = Field(ge=1, le=MAX_RESULT_POINTS)

    @field_validator("title", "description", "principal")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("result text must not be empty")
        return value

    @field_validator(
        "capabilities_before",
        "capabilities_gained",
        "capabilities_after",
        "data_fields",
        "evidence_refs",
    )
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item.strip()))

    @field_validator("credential_ref", "data_target_id")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


class RedTeamResultReportRequest(BaseModel):
    lease_id: str
    report: RedTeamResultReport


class RedTeamChain(BaseModel):
    id: str
    task_id: str
    goal_cycle_id: str | None = None
    title: str
    initial_principal: RedTeamPrincipal
    target_capabilities: list[str] = Field(default_factory=list)
    target_assets: list[str] = Field(default_factory=list)
    status: ChainStatus
    status_reason: str = ""
    current_state: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        normalized = str(value).strip()
        return LEGACY_CHAIN_STATUS_MAP.get(normalized, normalized)
    source_fact_ids: list[str] = Field(default_factory=list)
    source_intent_ids: list[str] = Field(default_factory=list)
    created_by: str
    created_at: str
    updated_at: str


class RedTeamResult(BaseModel):
    id: str
    task_id: str
    chain_id: str
    intent_id: str
    result_type: ResultType
    title: str
    description: str
    principal: str
    tenant: str
    capabilities_before: list[str] = Field(default_factory=list)
    capabilities_gained: list[str] = Field(default_factory=list)
    capabilities_after: list[str] = Field(default_factory=list)
    target_asset: str
    data_target_id: str | None = None
    sample_count: int | None = None
    sample_bytes: int | None = None
    data_fields: list[str] = Field(default_factory=list)
    data_summary: str = ""
    credential_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    status: ResultStatus
    reported_by: str
    points_declared: int = 0
    points_awarded: int = 0
    reviewed_by: str | None = None
    review_reason: str = ""
    created_at: str
    updated_at: str


class RedTeamResultReview(BaseModel):
    status: Literal["verified", "rejected", "duplicate", "needs-human"]
    reason: str = Field(min_length=4, max_length=8_000)
    reviewed_by: str = "reviewer"
    # 核定分：verified 时必填，其余状态忽略并置 0
    points_awarded: int | None = Field(default=None, ge=0, le=MAX_RESULT_POINTS)


# Reason/Plan own the operational lifecycle, the same way they own Todo
# status.  Points stay on verified Results; a completed Chain only means this
# path should not receive another Intent.
WorkerChainStatus = Literal["open", "partial", "blocked", "completed", "abandoned"]


class DraftChain(BaseModel):
    """One RedTeam Chain staged inside a Reason/Plan Workspace.

    The Chain is only persisted when the Workspace commits; ``ref`` is a
    Workspace-local handle so staged intent links can target it before an ID
    exists.  ``initial_principal`` defaults to the Task objective's initial
    identity when omitted.
    """

    ref: str
    title: str
    initial_principal: RedTeamPrincipal | None = None
    target_capabilities: list[str] = Field(default_factory=list, max_length=200)
    target_assets: list[str] = Field(default_factory=list, max_length=200)
    source_fact_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("ref", "title")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("target_capabilities", "target_assets", "source_fact_ids")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in values if item.strip()))


class DraftChainUpdate(BaseModel):
    """One status/title change staged against an already persisted Chain."""

    chain_id: str
    status: WorkerChainStatus | None = None
    status_reason: str | None = None
    title: str | None = None
    current_state: dict[str, Any] | None = None

    @field_validator("chain_id")
    @classmethod
    def require_chain_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("chain_id must not be empty")
        return value

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("status_reason")
    @classmethod
    def normalize_status_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @model_validator(mode="after")
    def require_change(self) -> "DraftChainUpdate":
        if (
            self.status is None
            and self.title is None
            and self.status_reason is None
            and self.current_state is None
        ):
            raise ValueError("chain update requires at least one changed field")
        return self
