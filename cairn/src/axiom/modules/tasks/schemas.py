from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Origin(BaseModel):
    host: str
    scheme: Literal["http", "https"] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    original_input: str


class TaskScope(BaseModel):
    enabled: bool = True
    targets: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    notes: str = ""


class CreateTask(BaseModel):
    project_id: str
    title: str
    origin: str
    goal: str
    hints: list[str] = Field(default_factory=list)
    guard_enabled: bool = False
    external_report_enabled: bool = False
    worker_pool: list[str] = Field(default_factory=list)
    scope: TaskScope | None = None

    @field_validator("project_id", "title", "origin", "goal")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("hints", "worker_pool")
    @classmethod
    def normalize_string_list(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = item.strip()
            if text and text not in seen:
                normalized.append(text)
                seen.add(text)
        return normalized


class UpdateTask(BaseModel):
    title: str | None = None
    goal: str | None = None
    guard_enabled: bool | None = None
    external_report_enabled: bool | None = None
    worker_pool: list[str] | None = None

    @field_validator("title", "goal")
    @classmethod
    def require_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("worker_pool")
    @classmethod
    def normalize_optional_worker_pool(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class UpdateTaskWorkerPool(BaseModel):
    worker_pool: list[str] = Field(default_factory=list)

    @field_validator("worker_pool")
    @classmethod
    def normalize_worker_pool(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class Task(BaseModel):
    id: str
    project_id: str
    title: str
    origin: Origin
    goal: str
    status: Literal["draft", "running", "stopped", "completed", "failed"]
    phase: Literal["bootstrap", "reason", "explore", "waiting", "none"]
    scope: TaskScope
    guard_enabled: bool
    external_report_enabled: bool
    worker_pool: list[str]
    legacy_readonly: bool = False
    reason_worker: str | None = None
    reason_heartbeat_at: str | None = None
    reason_activity_kind: str = ""
    reason_activity_summary: str = ""
    reason_activity_started_at: str | None = None
    reason_activity_updated_at: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    fact_count: int = 0
    intent_count: int = 0
    working_intent_count: int = 0
    hint_count: int = 0
    todo_count: int = 0
    vulnerability_count: int = 0


class TaskWorkerPoolUpdate(BaseModel):
    task: Task
    updated_unclaimed_intents: int
