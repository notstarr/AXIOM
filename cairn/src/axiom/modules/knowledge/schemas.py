from __future__ import annotations

from pydantic import BaseModel, field_validator


class CreateKnowledge(BaseModel):
    title: str = ""
    content: str
    hint: str = ""
    actor: str

    @field_validator("content", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("title", "hint")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()


class UpdateKnowledge(BaseModel):
    title: str | None = None
    content: str | None = None
    hint: str | None = None
    actor: str

    @field_validator("content")
    @classmethod
    def validate_optional_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("actor")
    @classmethod
    def validate_actor(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("title", "hint")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class Knowledge(BaseModel):
    id: str
    project_id: str
    title: str
    content: str
    hint: str
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str
    archived: bool = False


class CreateTaskHint(BaseModel):
    content: str
    actor: str

    @field_validator("content", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateTaskHint(BaseModel):
    content: str
    actor: str

    @field_validator("content", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class TaskHint(BaseModel):
    id: str
    task_id: str
    content: str
    creator: str
    created_at: str
    archived: bool = False
