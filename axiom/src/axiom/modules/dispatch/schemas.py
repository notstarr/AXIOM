from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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


class RecoveryReport(BaseModel):
    recovered: list[RecoveredIntent] = Field(default_factory=list)
    recovered_reason_runs: list[RecoveredReasonRun] = Field(default_factory=list)
