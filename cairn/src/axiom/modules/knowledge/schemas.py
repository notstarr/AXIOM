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


class TaskHint(BaseModel):
    id: str
    task_id: str
    content: str
    creator: str
    created_at: str
    archived: bool = False
