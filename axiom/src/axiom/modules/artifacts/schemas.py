from __future__ import annotations

from pydantic import BaseModel


class Artifact(BaseModel):
    id: str
    task_id: str
    intent_id: str | None = None
    artifact_type: str
    storage_key: str
    summary: str
    content_hash: str
    size: int
    created_by: str
    created_at: str
    archived: bool = False


class ArtifactContent(BaseModel):
    artifact: Artifact
    content: str
