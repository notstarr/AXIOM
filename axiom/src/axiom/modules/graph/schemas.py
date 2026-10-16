from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Fact(BaseModel):
    id: str
    task_id: str
    description: str
    created_at: str
    archived: bool = False


class Intent(BaseModel):
    id: str
    task_id: str
    todo_id: str | None = None
    chain_id: str | None = None
    kind: Literal["bootstrap", "reason", "explore"]
    from_facts: list[str]
    description: str
    priority: int = Field(default=50, ge=0, le=100)
    status: Literal[
        "pending",
        "claimed",
        "running",
        "concluding",
        "completed",
        "failed",
        "cancelled",
    ]
    creator: str
    worker: str | None = None
    worker_pool: list[str]
    targets: list[str]
    dedup_fingerprint: str = ""
    plan_metadata: dict[str, object] = Field(default_factory=dict)
    execute_timeout_seconds: int | None = None
    conclude_timeout_seconds: int | None = None
    lease_id: str | None = None
    claimed_at: str | None = None
    last_heartbeat_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0
    last_failure_kind: str = ""
    failure_class: str = ""
    activity_kind: str = ""
    activity_summary: str = ""
    activity_tool: str = ""
    activity_started_at: str | None = None
    activity_updated_at: str | None = None
    next_retry_at: str | None = None
    to_fact_id: str | None = None
    human_intervention_required: bool = False
    human_intervention_reason: str = ""
    human_intervention_marked_at: str | None = None
    assessment_status: Literal["open", "partial", "blocked", "completed"] = "open"
    assessment_reason: str = ""
    assessed_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    archived: bool = False


class TaskGraph(BaseModel):
    facts: list[Fact]
    intents: list[Intent]
    current_goal_cycle_id: str | None = None
    # Goal completion is a Reason decision, not a Fact relationship.  Keep
    # these fields on the graph payload so the UI can render the decision on
    # the Goal node without inventing a synthetic Fact -> Goal edge.
    goal_status: Literal["active", "completed"] = "active"
    goal_completed_at: str | None = None
    goal_completion_reason: str = ""
    goal_completion_evaluator: str = ""
    goal_completion_evaluation_id: str | None = None
    goal_completion_reason_run_id: str | None = None
    goal_from_fact_ids: list[str] = Field(default_factory=list)
    goal_history: list["GoalHistory"] = Field(default_factory=list)


class GoalHistory(BaseModel):
    goal_cycle_id: str
    sequence: int
    goal: str
    completed_at: str | None = None
    completion_reason: str = ""
    completion_evaluator: str = ""
    completion_evaluation_id: str | None = None
    completion_reason_run_id: str | None = None
    evidence_fact_ids: list[str] = Field(default_factory=list)


class GraphCounts(BaseModel):
    """轻量计数（不依赖完整数组）。"""

    facts: int = 0
    intents: int = 0


class GraphChildren(BaseModel):
    """按需展开：某个节点的直接下游（消费它的意图 + 这些意图产出的事实）。"""

    node_id: str
    facts: list[Fact] = Field(default_factory=list)
    intents: list[Intent] = Field(default_factory=list)
    child_count: int = 0
    has_more: bool = False


class UpdateFact(BaseModel):
    description: str | None = None
    archived: bool | None = None

    @field_validator("description")
    @classmethod
    def require_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("description must not be empty")
        return value


class UpdateIntent(BaseModel):
    description: str | None = None
    archived: bool | None = None

    @field_validator("description")
    @classmethod
    def require_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("description must not be empty")
        return value
