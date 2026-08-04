from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CreateIntervention(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    client_message_id: str = Field(min_length=1, max_length=160)
    actor: str = Field(default="human.frontend", max_length=120)

    @field_validator("message", "client_message_id", "actor")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class Intervention(BaseModel):
    id: str
    task_id: str
    intent_id: str
    lease_id: str
    actor: str
    message: str
    client_message_id: str
    status: Literal["pending", "sent", "accepted", "failed", "expired"]
    error: str = ""
    created_at: str
    sent_at: str | None = None
    updated_at: str


class ResolveIntervention(BaseModel):
    success: bool
    error: str = ""


class ConversationEvent(BaseModel):
    cursor: str
    kind: str
    summary: str
    detail: str = ""
    created_at: str = ""
    source: Literal["session", "intervention", "audit"]
    status: str = ""
    tool: str = ""
    intervention_id: str | None = None
