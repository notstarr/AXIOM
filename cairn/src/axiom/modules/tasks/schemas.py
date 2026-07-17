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
    created_at: str
    updated_at: str
    completed_at: str | None = None
