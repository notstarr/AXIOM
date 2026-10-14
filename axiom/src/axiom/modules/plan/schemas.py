from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.modules.redteam.schemas import (
    DraftChain,
    DraftChainIntentLink,
    DraftChainUpdate,
)
from axiom.modules.todos.schemas import (
    LEGACY_TODO_STATUS_MAP,
    LEGACY_TODO_STATUSES,
    TODO_STATUS_REASON_REQUIRED,
    TodoCategory,
    TodoSource,
    TodoStatus,
)


class DraftPlanTodo(BaseModel):
    ref: str
    title: str
    category: TodoCategory
    theme: str = ""
    priority: int = Field(default=50, ge=0, le=100)
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

    @field_validator("theme", "status_reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        return LEGACY_TODO_STATUS_MAP.get(str(value).strip(), str(value).strip())

    @model_validator(mode="after")
    def validate_shape(self) -> "DraftPlanTodo":
        if not self.sources:
            raise ValueError("Todo requires at least one source")
        if self.status in LEGACY_TODO_STATUSES:
            raise ValueError(f"legacy Todo status is read-only: {self.status}")
        if self.status in TODO_STATUS_REASON_REQUIRED and not self.status_reason:
            raise ValueError(f"status_reason is required for Todo status {self.status}")
        return self


class DraftPlanTodoUpdate(BaseModel):
    todo_id: str
    title: str | None = None
    category: TodoCategory | None = None
    theme: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    status: TodoStatus | None = None
    status_reason: str | None = None
    source_adds: list[TodoSource] = Field(default_factory=list, max_length=20)

    @field_validator("todo_id")
    @classmethod
    def require_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("todo_id must not be empty")
        return value

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("theme", "status_reason")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return LEGACY_TODO_STATUS_MAP.get(normalized, normalized)

    @model_validator(mode="after")
    def reject_legacy(self) -> "DraftPlanTodoUpdate":
        if self.status in LEGACY_TODO_STATUSES:
            raise ValueError(f"legacy Todo status is read-only: {self.status}")
        return self


class DraftPlanTodoDelete(BaseModel):
    todo_id: str
    reason: str

    @field_validator("todo_id", "reason")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class DraftPlanIntent(BaseModel):
    """A planned Explore action, with explicit provenance and stop criteria."""

    todo_ref: str | None = None
    todo_id: str | None = None
    # Optional Workspace-local handle so a staged RedTeam Chain can link this
    # Intent before either side has a persisted ID.
    ref: str | None = None
    from_facts: list[str] = Field(default_factory=list)
    description: str
    targets: list[str] = Field(default_factory=list)
    endpoint: str = ""
    test_vector: str = ""
    why_now: str
    source_refs: list[str] = Field(default_factory=list)
    expected_signal: str
    stop_condition: str
    dedup_fingerprint: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    # Runtime budgets are assigned by the backend.  Optional fields remain
    # accepted for rolling compatibility with older Plan workers.
    execute_seconds: int | None = None
    conclude_seconds: int | None = None

    @field_validator("description", "why_now", "expected_signal", "stop_condition")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("endpoint", "test_vector")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("ref")
    @classmethod
    def normalize_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("from_facts", "source_refs", "targets")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("dedup_fingerprint")
    @classmethod
    def normalize_fingerprint(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_todo_and_provenance(self) -> "DraftPlanIntent":
        if bool(self.todo_ref) == bool(self.todo_id):
            raise ValueError("exactly one of todo_ref or todo_id is required")
        if self.todo_ref:
            self.todo_ref = self.todo_ref.strip()
        if self.todo_id:
            self.todo_id = self.todo_id.strip()
        if not self.from_facts and not self.source_refs:
            raise ValueError("Plan Intent requires at least one source reference")
        return self


PlanDecisionKind = Literal["continue", "exhausted", "needs_reason", "dispatch_explore"]


class PlanDecision(BaseModel):
    decision: PlanDecisionKind
    reviewed_dimensions: list[str] = Field(default_factory=list)
    exhaustion_reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: str = "unspecified"

    @field_validator("reviewed_dimensions", "evidence_refs")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("exhaustion_reason", "confidence")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class PlanWorkspaceDraft(BaseModel):
    todo_adds: list[DraftPlanTodo] = Field(default_factory=list, max_length=50)
    todo_updates: list[DraftPlanTodoUpdate] = Field(default_factory=list, max_length=100)
    todo_deletions: list[DraftPlanTodoDelete] = Field(default_factory=list, max_length=100)
    intent_proposals: list[DraftPlanIntent] = Field(default_factory=list, max_length=256)
    decision: PlanDecision | None = None
    # Red-team staging.  Only valid for red_team tasks; Chains are persisted
    # when the Workspace commits, never at staging time.
    chain_adds: list[DraftChain] = Field(default_factory=list, max_length=20)
    chain_updates: list[DraftChainUpdate] = Field(default_factory=list, max_length=50)
    chain_intent_links: list[DraftChainIntentLink] = Field(
        default_factory=list, max_length=100
    )


PlanRunStatus = Literal[
    "pending", "claimed", "running", "concluding", "completed", "failed", "cancelled"
]


class PlanRun(BaseModel):
    id: str
    task_id: str
    goal_cycle_id: str | None = None
    status: PlanRunStatus
    trigger: str
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    input_fingerprint: str
    source_fact_ids: list[str] = Field(default_factory=list)
    worker_pool: list[str] = Field(default_factory=list)
    worker: str | None = None
    settings_version: int
    execute_timeout_seconds: int
    conclude_timeout_seconds: int
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
    decision: dict[str, Any] = Field(default_factory=dict)


class ClaimedPlanRun(BaseModel):
    task_id: str
    plan_run_id: str
    worker_name: str
    kind: Literal["plan"] = "plan"
    lease_id: str
    claimed_at: str
    status: Literal["claimed"] = "claimed"


class StartedPlanRun(BaseModel):
    task_id: str
    plan_run_id: str
    worker_name: str
    lease_id: str
    status: Literal["running"] = "running"


class ReleasedPlanRun(BaseModel):
    task_id: str
    plan_run_id: str
    status: Literal["pending", "failed"]
    next_retry_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0


class PlanWorkspace(BaseModel):
    id: str
    task_id: str
    plan_run_id: str
    lease_id: str
    status: Literal["open", "committed", "abandoned"]
    context_revision: int
    draft: PlanWorkspaceDraft
    worker: str
    failure_reason: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    committed_at: str | None = None


class PlanWorkspaceStatus(BaseModel):
    workspace_id: str
    status: Literal["open", "committed", "abandoned"]
    todo_add_count: int = 0
    todo_update_count: int = 0
    intent_proposal_count: int = 0
    chain_add_count: int = 0
    chain_update_count: int = 0
    chain_link_count: int = 0
    chain_refs: list[str] = Field(default_factory=list)
    decision: PlanDecision | None = None
    ready: bool = False
    errors: list[str] = Field(default_factory=list)


class PlanCommitResult(BaseModel):
    workspace: PlanWorkspace
    created_todo_ids: list[str] = Field(default_factory=list)
    updated_todo_ids: list[str] = Field(default_factory=list)
    deleted_todo_ids: list[str] = Field(default_factory=list)
    created_intent_ids: list[str] = Field(default_factory=list)
    created_chain_ids: list[str] = Field(default_factory=list)


class TriggerPlanRun(BaseModel):
    actor: str = "dispatcher.plan"
    trigger: str = "manual"
    trigger_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("actor", "trigger")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip() or "dispatcher.plan"


class PlanWorkspaceCommand(BaseModel):
    lease_id: str


class OpenPlanWorkspace(PlanWorkspaceCommand):
    plan_run_id: str
    context_revision: int = Field(ge=0)


class AbandonPlanWorkspace(PlanWorkspaceCommand):
    failure_reason: str = "Plan execution failed"


class AddPlanTodo(PlanWorkspaceCommand):
    todo: DraftPlanTodo


class UpdatePlanTodo(PlanWorkspaceCommand):
    update: DraftPlanTodoUpdate


class DeletePlanTodo(PlanWorkspaceCommand):
    deletion: DraftPlanTodoDelete


class ProposePlanIntent(PlanWorkspaceCommand):
    intent: DraftPlanIntent


class AddPlanChain(PlanWorkspaceCommand):
    chain: DraftChain


class UpdatePlanChain(PlanWorkspaceCommand):
    update: DraftChainUpdate


class LinkPlanChainIntent(PlanWorkspaceCommand):
    link: DraftChainIntentLink


class SubmitPlanDecision(PlanWorkspaceCommand):
    decision: PlanDecision


class PlanReconcileReport(BaseModel):
    created_plan_run_ids: list[str] = Field(default_factory=list)
