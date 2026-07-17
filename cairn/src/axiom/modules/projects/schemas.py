from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


class CreateProject(BaseModel):
    title: str
    description: str = ""

    @field_validator("description")
    @classmethod
    def trim_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("title")
    @classmethod
    def require_title(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("title must not be empty")
        return text


class Project(BaseModel):
    id: str
    title: str
    description: str = ""
    status: Literal["active", "archived"] = "active"
    created_at: str
    updated_at: str
