from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
TodoStatus = Literal["pending", "active", "completed", "rejected", "obsolete"]
TodoSourceType = Literal["fact", "hint", "knowledge", "artifact", "vulnerability", "origin"]


class TodoSource(BaseModel):
    type: TodoSourceType
    id: str

    @field_validator("id")
    @classmethod
    def require_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("source id must not be empty")
        return text


class CreateTodo(BaseModel):
    title: str
    category: TodoCategory
    theme: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    sources: list[TodoSource]
    actor: str

    @field_validator("title", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("theme")
    @classmethod
    def trim_theme(cls, value: str) -> str:
        return value.strip()


class UpdateTodo(BaseModel):
    title: str | None = None
    category: TodoCategory | None = None
    theme: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    status: TodoStatus | None = None
    status_reason: str | None = None
    actor: str

    @field_validator("actor")
    @classmethod
    def require_actor(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("actor must not be empty")
        return text

    @field_validator("title")
    @classmethod
    def validate_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("title must not be empty")
        return text

    @field_validator("theme", "status_reason")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


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
