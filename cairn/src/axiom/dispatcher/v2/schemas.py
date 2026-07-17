from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IntentSnapshot(BaseModel):
    id: str
    status: Literal[
        "pending", "claimed", "running", "concluding", "completed", "failed", "cancelled"
    ]
    creator: str
    retry_count: int = 0
    next_retry_at: str | None = None
    last_failure_kind: str = ""


class TaskSnapshot(BaseModel):
    id: str
    status: Literal["draft", "running", "stopped", "completed", "failed"]
    phase: Literal["bootstrap", "reason", "explore", "waiting", "none"]
    reason_worker: str | None = None
    reason_due: bool = False
    worker_pool: list[str] = Field(default_factory=list)
    intents: list[IntentSnapshot] = Field(default_factory=list)


class DispatchAction(BaseModel):
    kind: Literal["bootstrap", "reason", "explore", "needs_replan"]
    task_id: str
    intent_id: str | None = None


class WorkerSnapshot(BaseModel):
    name: str
    worker_type: str = "pi"
    capabilities: list[Literal["bootstrap", "reason", "explore"]]
    max_running: int
    running: int
    priority: int
    disabled: bool = False
    cooldown_until: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class DispatcherSnapshot(BaseModel):
    tasks: list[TaskSnapshot]
    workers: list[WorkerSnapshot]
