from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.modules.todos.schemas import TodoCategory, TodoSource, TodoStatus
from axiom.modules.goals.schemas import GoalAssessment
from axiom.modules.vulnerabilities.schemas import VulnerabilityReport


class DraftTodo(BaseModel):
    ref: str
    title: str
    category: TodoCategory
    theme: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    status: TodoStatus = "pending"
    status_reason: str = ""
    sources: list[TodoSource]

    @field_validator("ref", "title")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("theme", "status_reason")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_sources(self) -> "DraftTodo":
        if not self.sources:
            raise ValueError("Todo requires at least one source")
        return self


class DraftTodoUpdate(BaseModel):
    todo_id: str
    title: str | None = None
    category: TodoCategory | None = None
    theme: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    status: TodoStatus | None = None
    status_reason: str | None = None
    source_adds: list[TodoSource] = Field(default_factory=list)

    @field_validator("todo_id")
    @classmethod
    def require_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("todo_id must not be empty")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value


class DraftIntent(BaseModel):
    todo_ref: str | None = None
    todo_id: str | None = None
    from_facts: list[str]
    description: str
    targets: list[str]
    execute_seconds: int = Field(gt=0)
    conclude_seconds: int = Field(gt=0)

    @field_validator("description")
    @classmethod
    def require_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be empty")
        return value

    @field_validator("from_facts", "targets")
    @classmethod
    def normalize_required_list(cls, values: list[str]) -> list[str]:
        values = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not values:
            raise ValueError("must not be empty")
        return values

    @model_validator(mode="after")
    def require_one_todo_reference(self) -> "DraftIntent":
        if bool(self.todo_ref) == bool(self.todo_id):
            raise ValueError("exactly one of todo_ref or todo_id is required")
        if self.todo_ref:
            self.todo_ref = self.todo_ref.strip()
        if self.todo_id:
            self.todo_id = self.todo_id.strip()
        return self

    @model_validator(mode="after")
    def keep_budget_structured(self) -> "DraftIntent":
        lowered = self.description.casefold()
        if any(marker in lowered for marker in ("execute_seconds", "conclude_seconds", "timeout", "超时")):
            raise ValueError("Intent description must not contain timeout metadata")
        return self


class WorkspaceDraft(BaseModel):
    todo_adds: list[DraftTodo] = Field(default_factory=list, max_length=20)
    todo_updates: list[DraftTodoUpdate] = Field(default_factory=list, max_length=50)
    intent_proposals: list[DraftIntent] = Field(default_factory=list, max_length=8)
    vulnerability_reports: list[VulnerabilityReport] = Field(default_factory=list, max_length=20)
    goal_assessment: GoalAssessment | None = None


class ReasonWorkspace(BaseModel):
    id: str
    task_id: str
    intent_id: str
    lease_id: str
    status: Literal["open", "committed", "abandoned"]
    context_revision: int
    draft: WorkspaceDraft
    worker: str
    failure_reason: str = ""
    created_at: str
    updated_at: str
    committed_at: str | None = None


class OpenWorkspace(BaseModel):
    intent_id: str
    lease_id: str
    context_revision: int = Field(ge=0)


class WorkspaceWorkerCommand(BaseModel):
    lease_id: str


class AbandonWorkspace(WorkspaceWorkerCommand):
    failure_reason: str = "Reason execution failed"


class SubmitWorkspace(WorkspaceWorkerCommand):
    draft: WorkspaceDraft


class CommitResult(BaseModel):
    workspace: ReasonWorkspace
    created_todo_ids: list[str]
    created_intent_ids: list[str]
    created_vulnerability_ids: list[str] = Field(default_factory=list)
