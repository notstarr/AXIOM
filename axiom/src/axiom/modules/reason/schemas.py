from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from axiom.modules.goals.schemas import GoalAssessment
from axiom.modules.redteam.schemas import (
    DraftChain,
    DraftChainUpdate,
)
from axiom.modules.todos.schemas import (
    LEGACY_TODO_STATUS_MAP,
    LEGACY_TODO_STATUSES,
    TODO_STATUS_REASON_REQUIRED,
    TodoSource,
    TodoStatus,
)


class DraftTodo(BaseModel):
    ref: str
    title: str
    status: TodoStatus = "open"
    status_reason: str = ""
    sources: list[TodoSource]

    @field_validator("ref", "title")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("status_reason")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        normalized = str(value).strip()
        return LEGACY_TODO_STATUS_MAP.get(normalized, normalized)

    @model_validator(mode="after")
    def require_sources(self) -> "DraftTodo":
        if not self.sources:
            raise ValueError("Todo requires at least one source")
        return self

    @model_validator(mode="after")
    def require_status_reason(self) -> "DraftTodo":
        if self.status in LEGACY_TODO_STATUSES:
            raise ValueError(f"legacy Todo status is read-only: {self.status}")
        if self.status in TODO_STATUS_REASON_REQUIRED and not self.status_reason:
            raise ValueError(f"status_reason is required for Todo status {self.status}")
        return self


class DraftTodoUpdate(BaseModel):
    todo_id: str
    title: str | None = None
    status: TodoStatus | None = None
    status_reason: str | None = None
    source_adds: list[TodoSource] = Field(default_factory=list, max_length=10)

    @field_validator("todo_id")
    @classmethod
    def require_todo_id(cls, value: str) -> str:
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

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return LEGACY_TODO_STATUS_MAP.get(normalized, normalized)


    @model_validator(mode="after")
    def reject_legacy_status(self) -> "DraftTodoUpdate":
        if self.status in LEGACY_TODO_STATUSES:
            raise ValueError(f"legacy Todo status is read-only: {self.status}")
        return self


class DraftTodoDelete(BaseModel):
    todo_id: str
    reason: str

    @field_validator("todo_id", "reason")
    @classmethod
    def require_fields(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class DraftIntent(BaseModel):
    todo_ref: str | None = None
    todo_id: str | None = None
    # Red-team parent.  Exactly one chain reference is required for red_team
    # tasks; vulnerability tasks keep the Todo parent and must omit these.
    chain_ref: str | None = None
    chain_id: str | None = None
    # Optional Workspace-local handle so a staged Chain can be cited before
    # either side has a persisted ID.
    ref: str | None = None
    from_facts: list[str]
    description: str = Field(max_length=500)
    targets: list[str] = Field(default_factory=list)
    priority: int = Field(default=50, ge=0, le=100)
    # Runtime budgets are assigned by the backend.  Optional fields remain
    # accepted for rolling compatibility with older workers.
    execute_seconds: int | None = None
    conclude_seconds: int | None = None

    @field_validator("ref")
    @classmethod
    def normalize_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("description")
    @classmethod
    def require_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be empty")
        return value

    @field_validator("from_facts")
    @classmethod
    def normalize_required_list(cls, values: list[str]) -> list[str]:
        values = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not values:
            raise ValueError("must not be empty")
        return values

    @field_validator("targets")
    @classmethod
    def normalize_optional_targets(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("chain_ref", "chain_id")
    @classmethod
    def normalize_chain_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def normalize_parent_references(self) -> "DraftIntent":
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


IntentAssessmentStatus = Literal["open", "partial", "blocked", "completed"]


class DraftIntentAssessment(BaseModel):
    """One immutable evaluation of a terminal Explore Intent.

    ``open`` is the persisted/default value for an Intent which has not been
    evaluated yet.  Workspace commands may only transition it to one of the
    three terminal evaluation states; the service enforces that rule against
    the current database row as well as the in-memory draft.
    """

    intent_id: str
    status: IntentAssessmentStatus = Field(
        validation_alias=AliasChoices("status", "assessment_status")
    )
    reason: str = Field(
        validation_alias=AliasChoices("reason", "assessment_reason")
    )

    @field_validator("intent_id")
    @classmethod
    def require_intent_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("intent_id must not be empty")
        return value

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be empty")
        return value


# Public alias for callers that do not distinguish a staged assessment from
# the persisted Intent assessment payload.
IntentAssessment = DraftIntentAssessment


class WorkspaceDraft(BaseModel):
    todo_adds: list[DraftTodo] = Field(default_factory=list, max_length=20)
    todo_updates: list[DraftTodoUpdate] = Field(default_factory=list, max_length=50)
    todo_deletions: list[DraftTodoDelete] = Field(default_factory=list, max_length=50)
    # Reason is intentionally allowed to persist a large backlog in one run.
    # This is only an infrastructure guard against an accidentally unbounded
    # request; it is not a scheduling/concurrency quota.
    intent_proposals: list[DraftIntent] = Field(default_factory=list, max_length=256)
    intent_assessments: list[DraftIntentAssessment] = Field(
        default_factory=list, max_length=256
    )
    goal_assessment: GoalAssessment | None = None
    # Red-team staging.  Only valid for red_team tasks; Chains are persisted
    # when the Workspace commits, never at staging time.
    chain_adds: list[DraftChain] = Field(default_factory=list, max_length=20)
    chain_updates: list[DraftChainUpdate] = Field(default_factory=list, max_length=50)


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


class DeleteWorkspaceTodo(WorkspaceWorkerCommand):
    deletion: DraftTodoDelete


class ProposeWorkspaceIntent(WorkspaceWorkerCommand):
    intent: DraftIntent


class AssessWorkspaceIntent(WorkspaceWorkerCommand):
    assessment: DraftIntentAssessment


class AssessWorkspaceGoal(WorkspaceWorkerCommand):
    assessment: GoalAssessment


class AddWorkspaceChain(WorkspaceWorkerCommand):
    chain: DraftChain


class UpdateWorkspaceChain(WorkspaceWorkerCommand):
    update: DraftChainUpdate


class WorkspaceStatus(BaseModel):
    workspace_id: str
    status: Literal["open", "committed", "abandoned"]
    source_fact_ids: list[str] = Field(default_factory=list)
    todo_add_count: int = 0
    todo_update_count: int = 0
    intent_proposal_count: int = 0
    intent_assessment_count: int = 0
    todo_refs: list[str] = Field(default_factory=list)
    updated_todo_ids: list[str] = Field(default_factory=list)
    todo_review_ids: list[str] = Field(default_factory=list)
    chain_add_count: int = 0
    chain_update_count: int = 0
    chain_refs: list[str] = Field(default_factory=list)
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
    created_chain_ids: list[str] = Field(default_factory=list)


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
    goal_cycle_id: str | None = None
    status: ReasonRunStatus
    trigger: str
    # Read-only compatibility marker for rows created by the retired
    # Reason-based Plan implementation.  New Plan runs live in plan_runs.
    legacy_plan: bool = False
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    input_fingerprint: str
    source_fact_ids: list[str] = Field(default_factory=list)
    worker_pool: list[str] = Field(default_factory=list)
    worker: str | None = None
    settings_version: int
    execute_timeout_seconds: int
    conclude_timeout_seconds: int
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
