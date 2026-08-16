from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


TodoCategory = Literal[
    "recon",
    "authentication",
    "authorization",
    "injection",
    "business_logic",
    "file",
    "network",
    "configuration",
    "client_side",
    "other",
]
# ``rejected``/``obsolete`` are retained only so historical rows can still be
# rendered.  ``pending``/``active`` are accepted at the input boundary and
# normalized to the canonical four-state lifecycle below.
TodoStatus = Literal["open", "partial", "blocked", "completed", "rejected", "obsolete"]
LEGACY_TODO_STATUS_MAP = {"pending": "open", "active": "partial"}
TODO_STATUS_REASON_REQUIRED = {"partial", "blocked", "completed"}
LEGACY_TODO_STATUSES = {"rejected", "obsolete"}
TodoSourceType = Literal["fact", "hint", "artifact", "vulnerability", "knowledge", "origin"]


class TodoSource(BaseModel):
    type: TodoSourceType
    id: str

    @field_validator("id")
    @classmethod
    def require_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source id must not be empty")
        return value


class CreateTodo(BaseModel):
    title: str
    category: TodoCategory
    theme: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    status: TodoStatus = "open"
    status_reason: str = ""
    sources: list[TodoSource]
    actor: str = "human"

    @field_validator("title", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("theme")
    @classmethod
    def normalize_theme(cls, value: str) -> str:
        return value.strip()

    @field_validator("status_reason")
    @classmethod
    def normalize_status_reason(cls, value: str) -> str:
        return value.strip()

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        return LEGACY_TODO_STATUS_MAP.get(str(value).strip(), str(value).strip())

    @model_validator(mode="after")
    def require_sources(self) -> "CreateTodo":
        if not self.sources:
            raise ValueError("Todo requires at least one source")
        unique = dict.fromkeys((source.type, source.id) for source in self.sources)
        self.sources = [TodoSource(type=kind, id=source_id) for kind, source_id in unique]
        return self

    @model_validator(mode="after")
    def require_status_reason(self) -> "CreateTodo":
        if self.status in LEGACY_TODO_STATUSES:
            raise ValueError(f"legacy Todo status is read-only: {self.status}")
        if self.status in TODO_STATUS_REASON_REQUIRED and not self.status_reason:
            raise ValueError(f"status_reason is required for Todo status {self.status}")
        return self


class UpdateTodo(BaseModel):
    title: str | None = None
    category: TodoCategory | None = None
    theme: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    status: TodoStatus | None = None
    status_reason: str | None = None
    source_adds: list[TodoSource] = Field(default_factory=list)
    actor: str = "human"

    @field_validator("title")
    @classmethod
    def require_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("theme", "status_reason")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return LEGACY_TODO_STATUS_MAP.get(normalized, normalized)

    @field_validator("actor")
    @classmethod
    def require_actor(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be empty")
        return value

    @model_validator(mode="after")
    def reject_legacy_status(self) -> "UpdateTodo":
        if self.status in LEGACY_TODO_STATUSES:
            raise ValueError(f"legacy Todo status is read-only: {self.status}")
        return self

    @field_validator("source_adds")
    @classmethod
    def normalize_source_adds(cls, values: list[TodoSource]) -> list[TodoSource]:
        unique = dict.fromkeys((source.type, source.id) for source in values)
        return [TodoSource(type=kind, id=source_id) for kind, source_id in unique]


class Todo(BaseModel):
    id: str
    task_id: str
    title: str
    category: TodoCategory
    theme: str
    priority: int
    status: TodoStatus
    sources: list[TodoSource]
    created_by: str
    status_reason: str
    created_at: str
    updated_at: str
    completed_at: str | None = None
    archived: bool = False

    @field_validator("status", mode="before")
    @classmethod
    def normalize_persisted_status(cls, value: str) -> str:
        normalized = str(value).strip()
        return LEGACY_TODO_STATUS_MAP.get(normalized, normalized)
