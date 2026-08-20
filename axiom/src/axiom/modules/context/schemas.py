from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class BuildContext(BaseModel):
    phase: Literal["bootstrap", "reason", "explore", "plan", "review"]
    intent_id: str | None = None
    reason_run_id: str | None = None
    review_run_id: str | None = None
    lease_id: str | None = None


class ContextRevision(BaseModel):
    task_id: str
    revision: int
    phase: str
    intent_id: str | None = None
    reason_run_id: str | None = None
    review_run_id: str | None = None
    lease_id: str | None = None
    manifest_key: str
    manifest_path: str
    content_hash: str
    created_at: str


class ExpandContext(BaseModel):
    refs: list[str] = Field(min_length=1, max_length=100)

    @field_validator("refs")
    @classmethod
    def normalize_refs(cls, values: list[str]) -> list[str]:
        values = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not values:
            raise ValueError("refs must not be empty")
        return values


class ExpandedContext(BaseModel):
    task_id: str
    entries: dict[str, Any]
