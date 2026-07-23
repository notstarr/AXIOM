from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


class CreateProject(BaseModel):
    title: str
    description: str = ""

    @field_validator("title")
    @classmethod
    def require_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()


class UpdateProject(BaseModel):
    title: str | None = None
    description: str | None = None
    status: Literal["active", "archived"] | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class Project(BaseModel):
    id: str
    title: str
    description: str
    status: Literal["active", "archived"]
    created_at: str
    updated_at: str
    task_count: int = 0
    active_task_count: int = 0
    vulnerability_count: int = 0
    knowledge_count: int = 0
    credential_count: int = 0
