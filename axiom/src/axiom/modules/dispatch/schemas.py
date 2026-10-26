from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_serializer


class ClaimIntent(BaseModel):
    worker_name: str

    @field_validator("worker_name")
    @classmethod
    def require_worker_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("worker_name must not be empty")
        return value


class RecoverStaleIntents(BaseModel):
    stale_after_seconds: int = Field(default=60, ge=20, le=86_400)


class RecoveredIntent(BaseModel):
    task_id: str
    intent_id: str
    worker_name: str
    expired_lease_id: str


class RecoveredReasonRun(BaseModel):
    task_id: str
    reason_run_id: str
    worker_name: str
    expired_lease_id: str


class RecoveredReviewRun(BaseModel):
    task_id: str
    review_run_id: str
    worker_name: str
    expired_lease_id: str


class RecoveredPlanRun(BaseModel):
    task_id: str
    plan_run_id: str
    worker_name: str
    expired_lease_id: str


class AutoRecoveredWork(BaseModel):
    """One work item whose retry budget automatic recovery touched."""

    phase: str
    task_id: str
    run_id: str
    previous_infrastructure_retry_count: int = 0
    recovery_round: int = 0


class RecoveryReport(BaseModel):
    recovered: list[RecoveredIntent] = Field(default_factory=list)
    recovered_reason_runs: list[RecoveredReasonRun] = Field(default_factory=list)
    recovered_review_runs: list[RecoveredReviewRun] = Field(default_factory=list)
    recovered_plan_runs: list[RecoveredPlanRun] = Field(default_factory=list)
    # Automatic recovery is separate from stale-lease recovery: it re-arms items
    # that were parked, not leases that expired.  ``recovery_exhausted`` is the
    # subset where the automatic budget ran out and a human is now required.
    auto_recovered: list[AutoRecoveredWork] = Field(default_factory=list)
    recovery_exhausted: list[AutoRecoveredWork] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        """Keep the pre-Plan response shape when nothing Plan-shaped happened.

        The stale-lease report is consumed by the Runner every tick and used to
        be a bare ``recovered`` list; empty additions must not change its shape
        for callers that only read those keys.
        """

        payload = handler(self)
        if not self.recovered_plan_runs:
            payload.pop("recovered_plan_runs", None)
        if not self.auto_recovered:
            payload.pop("auto_recovered", None)
        if not self.recovery_exhausted:
            payload.pop("recovery_exhausted", None)
        return payload


class DispatchStallObservation(BaseModel):
    """One stall observation for a Task, reported by the Runner.

    The Runner owns the consecutive-tick timing because it already holds the
    snapshot; the service owns the audit record because that is where the
    project identity and the event log live.  ``ready`` and ``parked`` are
    counted separately because they mean different things: a ready row is
    waiting on capacity or a cooldown, a parked row is waiting on recovery.
    """

    task_id: str
    idle_seconds: int = Field(default=0, ge=0)
    ready_count: int = Field(default=0, ge=0)
    parked_count: int = Field(default=0, ge=0)
    recorded: bool = False

    @field_validator("task_id")
    @classmethod
    def require_task(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task_id must not be empty")
        return value
