from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


Capability = Literal["bootstrap", "reason", "explore"]


class UpsertWorker(BaseModel):
    name: str
    worker_type: str = "pi"
    capabilities: list[Capability]
    max_running: int = Field(gt=0)
    priority: int = Field(default=0, ge=0)
    config: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False

    @field_validator("name", "worker_type")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("capabilities")
    @classmethod
    def require_capabilities(cls, value: list[Capability]) -> list[Capability]:
        normalized = list(dict.fromkeys(value))
        if not normalized:
            raise ValueError("capabilities must not be empty")
        return normalized


class Worker(UpsertWorker):
    updated_at: str
    cooldown_until: str | None = None
    last_failure_kind: str = ""
