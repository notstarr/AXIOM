from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


CredentialType = Literal["headers", "cookie", "basic", "bearer", "ssh", "custom"]


class CredentialInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    credential_type: CredentialType
    target_host: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("name", "target_host")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("data")
    @classmethod
    def require_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("credential data must not be empty")
        return value


class Credential(CredentialInput):
    id: str
    project_id: str
    created_at: str
    updated_at: str


class CredentialMaterial(Credential):
    """The exact material delivered to a worker; no masking or encryption layer."""


class WorkerCredentialSummary(BaseModel):
    """Credential metadata safe to print in a Worker session."""

    id: str
    name: str
    credential_type: CredentialType
    target_host: str = ""
    data_fields: list[str] = Field(default_factory=list)
