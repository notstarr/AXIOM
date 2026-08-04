from __future__ import annotations

from pydantic import BaseModel


class Artifact(BaseModel):
    id: str
    task_id: str
    intent_id: str | None = None
    review_run_id: str | None = None
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


class ArtifactFolder(BaseModel):
    folder_key: str
    intent_id: str | None = None
    review_run_id: str | None = None
    intent_kind: str | None = None
    intent_status: str | None = None
    intent_description: str = ""
    artifact_count: int
    total_size: int
    latest_created_at: str
