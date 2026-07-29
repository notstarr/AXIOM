from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from axiom.modules.artifacts.schemas import Artifact


ArchiveRootType = Literal["fact", "intent", "artifact", "vulnerability", "todo"]
ArchiveRequestRootType = Literal["fact", "intent", "artifact", "vulnerability"]
ArchiveEntityType = Literal["fact", "intent", "artifact", "vulnerability", "todo"]
ArchiveRelation = Literal["root", "producer", "output", "descendant", "related"]


class ArchivePreviewRequest(BaseModel):
    root_type: ArchiveRequestRootType
    root_id: str

    @field_validator("root_id")
    @classmethod
    def require_root_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("root_id must not be empty")
        return value


class CreateArchiveBatch(ArchivePreviewRequest):
    expected_closure_hash: str
    reason: str = ""
    actor: str = "human.frontend"

    @field_validator("expected_closure_hash", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return value.strip()


class RestoreArchiveBatch(BaseModel):
    actor: str = "human.frontend"

    @field_validator("actor")
    @classmethod
    def require_actor(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be empty")
        return value


class ArchiveMember(BaseModel):
    entity_type: ArchiveEntityType
    entity_id: str
    relation: ArchiveRelation
    title: str = ""
    status: str = ""
    archived: bool = False


class ArchivePreview(BaseModel):
    task_id: str
    root_type: ArchiveRootType
    root_id: str
    closure_hash: str
    counts: dict[str, int] = Field(default_factory=dict)
    members: list[ArchiveMember] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class ArchiveBatch(ArchivePreview):
    id: str
    status: Literal["active", "restored"]
    reason: str
    actor: str
    created_at: str
    restored_at: str | None = None
    restored_by: str | None = None
    restored_count: int = 0
    retained_archived_count: int = 0


class ArchivedArtifactContent(BaseModel):
    artifact: Artifact
    content: str
