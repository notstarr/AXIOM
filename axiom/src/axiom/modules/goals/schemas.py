from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


class GoalAssessment(BaseModel):
    require_evidence_when_achieved: ClassVar[bool] = True

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

    @model_validator(mode="after")
    def achieved_requires_evidence(self) -> "GoalAssessment":
        if self.require_evidence_when_achieved and self.achieved and not self.evidence_refs:
            raise ValueError("An achieved Goal requires evidence refs")
        return self


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
    # Legacy completed Tasks truthfully record that their old runtime did not
    # persist evidence.  New input models remain strict; only the historical
    # read model permits an empty evidence list.
    require_evidence_when_achieved: ClassVar[bool] = False

    id: str
    task_id: str
    intent_id: str | None = None
    reason_run_id: str | None = None
    workspace_id: str | None = None
    evaluator: str
    created_at: str
