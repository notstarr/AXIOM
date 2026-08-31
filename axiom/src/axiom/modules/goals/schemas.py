from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class GoalAssessment(BaseModel):
    achieved: bool = False
    reason: str
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Goal assessment reason must not be empty")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def normalize_refs(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class AssessGoal(GoalAssessment):
    evaluator: str = "human"

    @field_validator("evaluator")
    @classmethod
    def require_evaluator(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("evaluator must not be empty")
        return value


class GoalEvaluation(GoalAssessment):
    id: str
    task_id: str
    goal_cycle_id: str | None = None
    intent_id: str | None = None
    reason_run_id: str | None = None
    workspace_id: str | None = None
    evaluator: str
    created_at: str


class GoalCycle(BaseModel):
    id: str
    task_id: str
    sequence: int
    goal: str
    status: Literal["active", "completed"]
    continuation_reason: str = ""
    focus: str = ""
    actor: str
    started_at: str
    completed_at: str | None = None
