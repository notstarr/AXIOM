from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RecordAuditEvent(BaseModel):
    project_id: str | None = None
    task_id: str | None = None
    intent_id: str | None = None
    workspace_id: str | None = None
    worker: str = ""
    event_type: str
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type", "message")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class AuditEvent(RecordAuditEvent):
    id: str
    created_at: str
