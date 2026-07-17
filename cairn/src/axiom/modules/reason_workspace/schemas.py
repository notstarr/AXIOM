from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.modules.todos.schemas import TodoCategory, TodoSource, TodoStatus


class DraftTodo(BaseModel):
    ref: str
    title: str
    category: TodoCategory
    theme: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    sources: list[TodoSource]

    @field_validator("ref", "title")
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

    @model_validator(mode="after")
    def require_sources(self) -> "DraftTodo":
        if not self.sources:
            raise ValueError("Todo requires at least one source")
        return self


class DraftTodoUpdate(BaseModel):
    todo_id: str
    title: str | None = None
    category: TodoCategory | None = None
    theme: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    status: TodoStatus | None = None
    status_reason: str | None = None

    @field_validator("todo_id")
    @classmethod
    def require_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("todo_id must not be empty")
        return text


class DraftIntent(BaseModel):
    todo_ref: str | None = None
    todo_id: str | None = None
    from_facts: list[str]
    description: str
    targets: list[str]
    execute_seconds: int = Field(gt=0)
    conclude_seconds: int = Field(gt=0)

    @field_validator("description")
    @classmethod
    def require_description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("description must not be empty")
        return text

    @field_validator("from_facts", "targets")
    @classmethod
    def normalize_list(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @model_validator(mode="after")
    def require_one_todo_reference(self) -> "DraftIntent":
        if bool(self.todo_ref) == bool(self.todo_id):
            raise ValueError("exactly one of todo_ref or todo_id is required")
        if self.todo_ref:
            self.todo_ref = self.todo_ref.strip()
        if self.todo_id:
            self.todo_id = self.todo_id.strip()
        return self


class WorkspaceDraft(BaseModel):
    todo_adds: list[DraftTodo] = Field(default_factory=list)
    todo_updates: list[DraftTodoUpdate] = Field(default_factory=list)
    intent_proposals: list[DraftIntent] = Field(default_factory=list)


class ReasonWorkspace(BaseModel):
    id: str
    task_id: str
    status: Literal["open", "committed", "abandoned"]
    context_revision: int
    draft: WorkspaceDraft
    worker: str
    failure_reason: str = ""
    created_at: str
    updated_at: str
    committed_at: str | None = None


class CommitResult(BaseModel):
    workspace: ReasonWorkspace
    created_todo_ids: list[str]
    created_intent_ids: list[str]
