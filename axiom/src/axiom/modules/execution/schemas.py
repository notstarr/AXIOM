from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.vulnerabilities.schemas import ExploreVulnerabilityReport, Vulnerability


class LeaseCommand(BaseModel):
    lease_id: str

    @field_validator("lease_id")
    @classmethod
    def require_lease(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("lease_id must not be empty")
        return value


class CredentialAccessCommand(LeaseCommand):
    intent_id: str | None = None
    reason_run_id: str | None = None

    @field_validator("intent_id", "reason_run_id")
    @classmethod
    def normalize_owner_id(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def require_one_execution_owner(self) -> "CredentialAccessCommand":
        if bool(self.intent_id) == bool(self.reason_run_id):
            raise ValueError("exactly one of intent_id or reason_run_id is required")
        return self


class ActivityCommand(LeaseCommand):
    activity_kind: str = Field(min_length=1, max_length=40)
    activity_summary: str = Field(min_length=1, max_length=500)
    activity_tool: str = Field(default="", max_length=120)

    @field_validator("activity_kind", "activity_summary")
    @classmethod
    def require_activity_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("activity text must not be empty")
        return value

    @field_validator("activity_tool")
    @classmethod
    def normalize_activity_tool(cls, value: str) -> str:
        return value.strip()


class ReleaseIntentCommand(LeaseCommand):
    failure_kind: str = ""
    failure_class: Literal["execution", "transient"] = "execution"
    next_retry_at: str | None = None


class ExecutionInput(BaseModel):
    task_id: str
    project_id: str = ""
    intent_id: str | None = None
    reason_run_id: str | None = None
    lease_id: str
    worker_name: str
    intent_kind: Literal["bootstrap", "reason", "explore"] = "bootstrap"
    intent_description: str = ""
    intent_targets: list[str] = Field(default_factory=list)
    intent_source_facts: list[str] = Field(default_factory=list)
    todo_id: str | None = None
    origin: str
    goal: str
    hints: list[str] = Field(default_factory=list)
    skill_directories: list[str] = Field(default_factory=list)
    guard_enabled: bool = False
    scope: dict[str, object]
    run_dir: str
    execute_timeout_seconds: int | None = None
    conclude_timeout_seconds: int | None = None
    context_revision: int | None = None
    context_manifest_path: str | None = None
    axiom_tool_keys: list[str] = Field(default_factory=list)
    axiom_tool_extensions: list[str] = Field(default_factory=list)
    axiom_tool_snapshot_path: str | None = None

    @model_validator(mode="after")
    def require_one_execution_owner(self) -> "ExecutionInput":
        if bool(self.intent_id) == bool(self.reason_run_id):
            raise ValueError("exactly one of intent_id or reason_run_id is required")
        if self.intent_kind == "reason" and not self.reason_run_id:
            raise ValueError("Reason execution requires reason_run_id")
        if self.intent_kind != "reason" and not self.intent_id:
            raise ValueError("Intent execution requires intent_id")
        return self


class StartedIntent(BaseModel):
    task_id: str
    intent_id: str
    worker_name: str
    lease_id: str
    status: Literal["running"] = "running"


class CompleteBootstrapCommand(LeaseCommand):
    """Close Bootstrap using files already present in the leased run directory."""


class CompleteExploreCommand(LeaseCommand):
    """Close Explore using files and code-maintained state from the run directory."""


class ReportExploreVulnerabilityCommand(LeaseCommand):
    report: ExploreVulnerabilityReport


class ExploreVulnerabilityRecorded(BaseModel):
    total: int
    duplicate: bool = False


class ExploreCompleted(BaseModel):
    task_id: str
    intent_id: str
    fact_id: str
    reason_run_id: str | None = None
    artifacts: list[Artifact]
    vulnerabilities: list[Vulnerability] = Field(default_factory=list)
    status: Literal["completed"] = "completed"


class BootstrapCompleted(BaseModel):
    task_id: str
    intent_id: str
    fact_id: str
    next_reason_run_id: str | None = None
    artifacts: list[Artifact]
    status: Literal["completed"] = "completed"


class ReleasedIntent(BaseModel):
    task_id: str
    intent_id: str
    status: Literal["pending", "failed"] = "pending"
    next_retry_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0
