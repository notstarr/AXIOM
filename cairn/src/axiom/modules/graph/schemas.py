from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


IntentStatus = Literal[
    "pending",
    "claimed",
    "running",
    "concluding",
    "completed",
    "failed",
    "cancelled",
]


class Fact(BaseModel):
    id: str
    task_id: str
    description: str
    archived: bool = False
    created_at: str


class Intent(BaseModel):
    id: str
    task_id: str
    todo_id: str | None = None
    from_facts: list[str] = Field(default_factory=list)
    to_fact_id: str | None = None
    description: str
    status: IntentStatus
    creator: str
    worker: str | None = None
    worker_pool: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    execute_timeout_seconds: int | None = None
    conclude_timeout_seconds: int | None = None
    last_heartbeat_at: str | None = None
    activity_kind: str = ""
    activity_summary: str = ""
    activity_tool: str = ""
    activity_started_at: str | None = None
    activity_updated_at: str | None = None
    retry_count: int = 0
    infrastructure_retry_count: int = 0
    next_retry_at: str | None = None
    last_failure_kind: str = ""
    failure_class: str = ""
    created_at: str
    concluded_at: str | None = None
    archived: bool = False


class ConcludeResult(BaseModel):
    fact: Fact
    intent: Intent
