from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.modules.goals.schemas import GoalAssessment
from axiom.modules.todos.schemas import TodoCategory, TodoSource, TodoStatus


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
    goal_assessment: GoalAssessment | None = None


class ReasonWorkspace(BaseModel):
    id: str
    task_id: str
    intent_id: str | None = None
    reason_run_id: str | None = None
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
    reason_run_id: str
    lease_id: str
    context_revision: int = Field(ge=0)


class TriggerReasonRun(BaseModel):
    actor: str = "人工"

    @field_validator("actor")
    @classmethod
    def normalize_actor(cls, value: str) -> str:
        return value.strip() or "人工"


class WorkspaceWorkerCommand(BaseModel):
    lease_id: str


class AbandonWorkspace(WorkspaceWorkerCommand):
    failure_reason: str = "Reason execution failed"


class AddWorkspaceTodo(WorkspaceWorkerCommand):
    todo: DraftTodo


class UpdateWorkspaceTodo(WorkspaceWorkerCommand):
    update: DraftTodoUpdate


class ProposeWorkspaceIntent(WorkspaceWorkerCommand):
    intent: DraftIntent


class AssessWorkspaceGoal(WorkspaceWorkerCommand):
    assessment: GoalAssessment


class WorkspaceStatus(BaseModel):
    workspace_id: str
    status: Literal["open", "committed", "abandoned"]
    source_fact_ids: list[str] = Field(default_factory=list)
    todo_add_count: int = 0
    todo_update_count: int = 0
    intent_proposal_count: int = 0
    todo_refs: list[str] = Field(default_factory=list)
    updated_todo_ids: list[str] = Field(default_factory=list)
    todo_review_ids: list[str] = Field(default_factory=list)
    goal_assessed: bool = False
    goal_achieved: bool = False
    open_explore_intents: int = 0
    proposal_limit: int = 0
    remaining_intent_quota: int = 0
    ready: bool = False
    errors: list[str] = Field(default_factory=list)


class CommitResult(BaseModel):
    workspace: ReasonWorkspace
    created_todo_ids: list[str]
    created_intent_ids: list[str]


ReasonRunStatus = Literal[
    "pending",
    "claimed",
    "running",
    "concluding",
    "completed",
    "failed",
    "cancelled",
]


class ReasonRun(BaseModel):
    id: str
    task_id: str
    status: ReasonRunStatus
    trigger: str
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    input_fingerprint: str
    source_fact_ids: list[str] = Field(default_factory=list)
    worker_pool: list[str] = Field(default_factory=list)
    worker: str | None = None
    settings_version: int
    execute_timeout_seconds: int
    conclude_timeout_seconds: int
    target_open_intents: int
    max_new_intents: int
    explore_execute_min_seconds: int
    explore_execute_max_seconds: int
    explore_conclude_min_seconds: int
    explore_conclude_max_seconds: int
    lease_id: str | None = None
    claimed_at: str | None = None
    last_heartbeat_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0
    next_retry_at: str | None = None
    last_failure_kind: str = ""
    failure_class: str = ""
    activity_kind: str = ""
    activity_summary: str = ""
    activity_tool: str = ""
    activity_started_at: str | None = None
    activity_updated_at: str | None = None
    created_at: str
    updated_at: str
    concluded_at: str | None = None


class ClaimedReasonRun(BaseModel):
    task_id: str
    reason_run_id: str
    worker_name: str
    kind: Literal["reason"] = "reason"
    lease_id: str
    claimed_at: str
    status: Literal["claimed"] = "claimed"


class StartedReasonRun(BaseModel):
    task_id: str
    reason_run_id: str
    worker_name: str
    lease_id: str
    status: Literal["running"] = "running"


class ReleasedReasonRun(BaseModel):
    task_id: str
    reason_run_id: str
    status: Literal["pending", "failed"]
    next_retry_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0


class ReasonReconcileReport(BaseModel):
    created_reason_run_ids: list[str] = Field(default_factory=list)
