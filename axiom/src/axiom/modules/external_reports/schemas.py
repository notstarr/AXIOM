from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from axiom.modules.vulnerabilities.schemas import Severity, VulnerabilityStatus


ExternalReportStatus = Literal["running", "succeeded", "failed"]


class ExternalArtifactEvidence(BaseModel):
    id: str
    name: str
    artifact_type: str
    media_type: str
    summary: str = ""
    size: int
    sha256: str
    content: str | None = None


class ExternalVulnerabilityPayload(BaseModel):
    schema_version: Literal[2] = 2
    title: str
    type: str
    severity: Severity
    affected_target: str = ""
    description: str
    prerequisites: str = ""
    impact: str
    remediation: str = ""
    status: VulnerabilityStatus
    status_reason: str = ""
    intent_id: str | None = None
    source: str
    reported_by: str
    finding: ExternalArtifactEvidence | None = None
    evidence: list[ExternalArtifactEvidence] = Field(default_factory=list)


class ExternalReportPreview(BaseModel):
    method: Literal["POST"] = "POST"
    endpoint: str
    payload: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)


class ExternalReportAttempt(BaseModel):
    id: str
    task_id: str
    vulnerability_id: str
    attempt_no: int
    status: ExternalReportStatus
    endpoint: str
    payload: dict[str, Any]
    response_status: int | None = None
    response_body: str = ""
    error: str = ""
    created_at: str
    updated_at: str
    completed_at: str | None = None


class ExternalReportResult(BaseModel):
    attempt: ExternalReportAttempt
    accepted: bool


class ExternalReportConfiguration(BaseModel):
    enabled: bool
    endpoint_configured: bool
    token_configured: bool
    max_attempts: int
