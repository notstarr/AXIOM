from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CredentialInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    content: str
    notes: str | None = None
    refresh_content: str | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("credential name must not be empty")
        if "\n" in value or "\r" in value:
            raise ValueError("credential name must be a single line")
        return value

    @field_validator("content")
    @classmethod
    def require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("credential content must not be empty")
        return value

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @field_validator("refresh_content")
    @classmethod
    def normalize_refresh_content(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value


class SetCredentialEnabled(BaseModel):
    enabled: bool


class CredentialSummary(BaseModel):
    id: str
    project_id: str
    name: str
    notes: str | None = None
    refresh_id: str | None = None
    enabled: bool
    created_at: str
    updated_at: str


class Credential(CredentialSummary):
    content: str


class CredentialRefresh(BaseModel):
    id: str
    credential_id: str
    content: str
    created_at: str
    updated_at: str


class CredentialMaterial(BaseModel):
    """Credential material disclosed to one active Worker lease."""

    id: str
    name: str
    content: str
    notes: str | None = None
    refresh_id: str | None = None


class WorkerCredentialSummary(BaseModel):
    """Credential metadata safe to print in a Worker session."""

    id: str
    name: str


class WorkerCredentialRefresh(BaseModel):
    """Refresh instructions disclosed through one active Worker lease."""

    id: str
    content: str
