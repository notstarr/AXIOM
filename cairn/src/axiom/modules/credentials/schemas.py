from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class CreateCredential(BaseModel):
    label: str
    auth_type: str
    data: dict[str, Any]
    domains: list[str] = Field(default_factory=list)
    notes: str = ""
    active: bool = True

    @field_validator("label", "auth_type")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))

    @field_validator("notes")
    @classmethod
    def trim_notes(cls, value: str) -> str:
        return value.strip()


class UpdateCredential(BaseModel):
    label: str | None = None
    auth_type: str | None = None
    data: dict[str, Any] | None = None
    domains: list[str] | None = None
    notes: str | None = None
    active: bool | None = None

    @field_validator("label", "auth_type")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("domains")
    @classmethod
    def normalize_optional_domains(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))

    @field_validator("notes")
    @classmethod
    def trim_optional_notes(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class Credential(BaseModel):
    id: str
    project_id: str
    label: str
    auth_type: str
    data: dict[str, Any]
    domains: list[str]
    notes: str
    active: bool
    created_at: str
    updated_at: str


class CredentialSummary(BaseModel):
    id: str
    project_id: str
    label: str
    auth_type: str
    domains: list[str]
    notes: str
    active: bool
