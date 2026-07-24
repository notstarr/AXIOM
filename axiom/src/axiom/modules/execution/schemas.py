from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from axiom.modules.artifacts.schemas import Artifact, BootstrapBundle, ExploreBundle
from axiom.modules.credentials.schemas import CredentialMaterial
from axiom.modules.vulnerabilities.schemas import Vulnerability


class LeaseCommand(BaseModel):
    lease_id: str

    @field_validator("lease_id")
    @classmethod
    def require_lease(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("lease_id must not be empty")
        return value


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
    intent_id: str
    lease_id: str
    worker_name: str
    intent_kind: Literal["bootstrap", "reason", "explore"] = "bootstrap"
    intent_description: str = ""
    intent_targets: list[str] = Field(default_factory=list)
    intent_source_facts: list[str] = Field(default_factory=list)
    todo_id: str | None = None
    credentials: list[CredentialMaterial] = Field(default_factory=list)
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


class StartedIntent(BaseModel):
    task_id: str
    intent_id: str
    worker_name: str
    lease_id: str
    status: Literal["running"] = "running"


class CompleteBootstrapCommand(LeaseCommand):
    summary: str
    artifacts: BootstrapBundle

    @field_validator("summary")
    @classmethod
    def require_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value


class CompleteExploreCommand(LeaseCommand):
    output: ExploreBundle


class ExploreCompleted(BaseModel):
    task_id: str
    intent_id: str
    fact_id: str
    reason_intent_id: str | None = None
    artifacts: list[Artifact]
    vulnerabilities: list[Vulnerability] = Field(default_factory=list)
    status: Literal["completed"] = "completed"


class BootstrapCompleted(BaseModel):
    task_id: str
    intent_id: str
    fact_id: str
    next_intent_id: str
    artifacts: list[Artifact]
    status: Literal["completed"] = "completed"


class ReleasedIntent(BaseModel):
    task_id: str
    intent_id: str
    status: Literal["pending", "failed"] = "pending"
    next_retry_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0
