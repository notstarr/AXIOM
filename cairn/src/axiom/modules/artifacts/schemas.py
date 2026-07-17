from __future__ import annotations

from pydantic import BaseModel, field_validator


class RegisterArtifact(BaseModel):
    intent_id: str | None = None
    artifact_type: str
    path: str
    summary: str = ""
    actor: str

    @field_validator("artifact_type", "path", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("intent_id", "summary")
    @classmethod
    def trim_optional(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class Artifact(BaseModel):
    id: str
    task_id: str
    intent_id: str | None
    artifact_type: str
    path: str
    summary: str
    content_hash: str
    size: int
    created_by: str
    created_at: str
    archived: bool = False
