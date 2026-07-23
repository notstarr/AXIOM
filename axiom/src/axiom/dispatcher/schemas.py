from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


IntentKind = Literal["bootstrap", "reason", "explore"]


class IntentSnapshot(BaseModel):
    task_id: str
    id: str
    kind: IntentKind
    status: Literal[
        "pending",
        "claimed",
        "running",
        "concluding",
        "completed",
        "failed",
        "cancelled",
    ]
    worker_pool: list[str] = Field(default_factory=list)
    next_retry_at: datetime | None = None
    created_at: datetime


class TaskSnapshot(BaseModel):
    id: str
    status: Literal["running", "stopped", "completed", "failed"]
    intents: list[IntentSnapshot] = Field(default_factory=list)


class WorkerSnapshot(BaseModel):
    name: str
    capabilities: frozenset[IntentKind] = frozenset()
    max_running: int = Field(gt=0)
    running: int = Field(default=0, ge=0)
    priority: int = 0
    disabled: bool = False
    cooldown_until: datetime | None = None


class DispatcherSnapshot(BaseModel):
    tasks: list[TaskSnapshot] = Field(default_factory=list)
    workers: list[WorkerSnapshot] = Field(default_factory=list)


class ClaimAction(BaseModel):
    task_id: str
    intent_id: str
    worker_name: str


class DispatchAction(BaseModel):
    kind: Literal["claim"]
    claim: ClaimAction


class ClaimedIntent(BaseModel):
    task_id: str
    intent_id: str
    worker_name: str
    kind: IntentKind
    status: Literal["claimed"] = "claimed"
    claimed_at: str
    lease_id: str


class DispatchReport(BaseModel):
    considered: int = 0
    planned: int = 0
    claimed: int = 0
    conflicts: int = 0
    claims: list[ClaimedIntent] = Field(default_factory=list)
