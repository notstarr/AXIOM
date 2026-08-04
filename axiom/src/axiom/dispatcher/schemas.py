from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from axiom.modules.reason.schemas import ClaimedReasonRun
from axiom.modules.vulnerability_reviews.schemas import ClaimedVulnerabilityReviewRun


IntentKind = Literal["bootstrap", "reason", "explore"]
SchedulableIntentKind = Literal["bootstrap", "explore"]
WorkKind = Literal["bootstrap", "reason", "explore", "review"]
ACTIVE_WORK_STATUSES = frozenset({"claimed", "running", "concluding"})


class IntentSnapshot(BaseModel):
    task_id: str
    id: str
    kind: IntentKind
    status: Literal[
        "pending",
        "claimed",
        "running",
        "concluding",
        "completed",
        "failed",
        "cancelled",
    ]
    worker_pool: list[str] = Field(default_factory=list)
    next_retry_at: datetime | None = None
    created_at: datetime
    claimed_at: datetime | None = None


class ReasonRunSnapshot(BaseModel):
    task_id: str
    id: str
    kind: Literal["reason"] = "reason"
    status: Literal[
        "pending",
        "claimed",
        "running",
        "concluding",
        "completed",
        "failed",
        "cancelled",
    ]
    worker_pool: list[str] = Field(default_factory=list)
    next_retry_at: datetime | None = None
    created_at: datetime
    claimed_at: datetime | None = None


class ReviewRunSnapshot(BaseModel):
    task_id: str
    id: str
    vulnerability_id: str
    kind: Literal["review"] = "review"
    status: Literal[
        "pending",
        "claimed",
        "running",
        "concluding",
        "completed",
        "failed",
        "cancelled",
    ]
    worker_pool: list[str] = Field(default_factory=list)
    next_retry_at: datetime | None = None
    created_at: datetime
    claimed_at: datetime | None = None


class TaskSnapshot(BaseModel):
    id: str
    status: Literal["running", "stopped", "completed", "failed"]
    created_at: datetime = Field(
        default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc)
    )
    intents: list[IntentSnapshot] = Field(default_factory=list)
    reason_runs: list[ReasonRunSnapshot] = Field(default_factory=list)
    review_runs: list[ReviewRunSnapshot] = Field(default_factory=list)
    last_claimed_at: datetime | None = None


class WorkerSnapshot(BaseModel):
    name: str
    capabilities: frozenset[WorkKind] = frozenset()
    max_running: int = Field(gt=0)
    running: int = Field(default=0, ge=0)
    priority: int = 0
    disabled: bool = False
    cooldown_until: datetime | None = None


class DispatchLimits(BaseModel):
    config_version: int = 1
    global_max_active_runs: int = Field(default=10, ge=1)
    global_max_active_tasks: int = Field(default=3, ge=1)
    per_task_max_active_runs: int = Field(default=4, ge=1)
    per_task_explore_concurrency: int = Field(default=4, ge=1)


class DispatcherSnapshot(BaseModel):
    tasks: list[TaskSnapshot] = Field(default_factory=list)
    workers: list[WorkerSnapshot] = Field(default_factory=list)
    limits: DispatchLimits = Field(default_factory=DispatchLimits)


class ClaimAction(BaseModel):
    task_id: str
    worker_name: str
    kind: WorkKind
    intent_id: str | None = None
    reason_run_id: str | None = None
    review_run_id: str | None = None

    @model_validator(mode="after")
    def require_one_owner(self) -> "ClaimAction":
        if sum(bool(item) for item in (self.intent_id, self.reason_run_id, self.review_run_id)) != 1:
            raise ValueError("claim requires exactly one Intent, ReasonRun or ReviewRun")
        if self.kind == "reason" and not self.reason_run_id:
            raise ValueError("Reason claims require reason_run_id")
        if self.kind == "review" and not self.review_run_id:
            raise ValueError("Review claims require review_run_id")
        if self.kind not in {"reason", "review"} and not self.intent_id:
            raise ValueError("Intent claims require intent_id")
        return self


class DispatchAction(BaseModel):
    kind: Literal["claim"]
    claim: ClaimAction


class ClaimedIntent(BaseModel):
    task_id: str
    intent_id: str
    worker_name: str
    kind: IntentKind
    status: Literal["claimed"] = "claimed"
    claimed_at: str
    lease_id: str


ClaimedWork = ClaimedIntent | ClaimedReasonRun | ClaimedVulnerabilityReviewRun


class DispatchReport(BaseModel):
    considered: int = 0
    planned: int = 0
    claimed: int = 0
    conflicts: int = 0
    claims: list[ClaimedWork] = Field(default_factory=list)
