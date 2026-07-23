from __future__ import annotations

from pydantic import BaseModel, field_validator


class CreateKnowledge(BaseModel):
    title: str
    content: str
    category: str = "note"
    created_by: str = "human"
    enabled: bool = True

    @field_validator("title", "content", "category", "created_by")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class UpdateKnowledge(BaseModel):
    title: str | None = None
    content: str | None = None
    category: str | None = None
    enabled: bool | None = None
    archived: bool | None = None

    @field_validator("title", "content", "category")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class Knowledge(BaseModel):
    id: str
    project_id: str
    title: str
    content: str
    category: str
    created_by: str
    created_at: str
    updated_at: str
    enabled: bool = True
    archived: bool = False


class KnowledgeIndexEntry(BaseModel):
    id: str
    title: str
    category: str
    preview: str
    ref: str
    detail: str
