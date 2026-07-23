from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


Severity = Literal["critical", "high", "medium", "low", "info"]
VulnerabilityStatus = Literal["待验证", "已验证", "忽略", "重复"]


class ReportVulnerability(BaseModel):
    intent_id: str | None = None
    title: str
    type: str
    severity: Severity
    description: str
    prerequisites: str = ""
    impact: str
    reproduction_steps: list[str] = Field(default_factory=list)
    remediation: str = ""
    curl_command: str = ""
    evidence: list[str] = Field(default_factory=list)
    affected_target: str = ""
    status: VulnerabilityStatus = "待验证"
    status_reason: str = ""
    source: str
    reported_by: str

    @field_validator("title", "type", "description", "impact", "source", "reported_by")
    @classmethod
    def require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator(
        "intent_id",
        "prerequisites",
        "remediation",
        "curl_command",
        "affected_target",
        "status_reason",
    )
    @classmethod
    def trim_optional(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("reproduction_steps", "evidence")
    @classmethod
    def normalize_list(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class Vulnerability(ReportVulnerability):
    id: str
    task_id: str
    created_at: str
    updated_at: str
    archived: bool = False


class UpdateVulnerability(BaseModel):
    title: str | None = None
    type: str | None = None
    severity: Severity | None = None
    description: str | None = None
    prerequisites: str | None = None
    impact: str | None = None
    reproduction_steps: list[str] | None = None
    remediation: str | None = None
    curl_command: str | None = None
    evidence: list[str] | None = None
    affected_target: str | None = None
    status: VulnerabilityStatus | None = None
    status_reason: str | None = None
    source: str | None = None
    reported_by: str | None = None

    @field_validator("title", "type", "description", "impact", "source", "reported_by")
    @classmethod
    def validate_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator(
        "prerequisites",
        "remediation",
        "curl_command",
        "affected_target",
        "status_reason",
    )
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("reproduction_steps", "evidence")
    @classmethod
    def normalize_optional_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item.strip() for item in value if item.strip()]
