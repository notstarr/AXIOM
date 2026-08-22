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


class RecoveryReport(BaseModel):
    recovered: list[RecoveredIntent] = Field(default_factory=list)
    recovered_reason_runs: list[RecoveredReasonRun] = Field(default_factory=list)
    recovered_review_runs: list[RecoveredReviewRun] = Field(default_factory=list)
    recovered_plan_runs: list[RecoveredPlanRun] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        """Keep the pre-Plan response shape when no Plan was recovered."""

        payload = handler(self)
        if not self.recovered_plan_runs:
            payload.pop("recovered_plan_runs", None)
        return payload
