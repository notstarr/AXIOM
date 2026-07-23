from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from axiom.modules.vulnerabilities.schemas import VulnerabilityReport


ExternalReportStatus = Literal["running", "succeeded", "failed"]


class ExternalReportPreview(BaseModel):
    method: Literal["POST"] = "POST"
    endpoint: str
    payload: VulnerabilityReport
    headers: dict[str, str] = Field(default_factory=dict)


class ExternalReportAttempt(BaseModel):
    id: str
    task_id: str
    vulnerability_id: str
    attempt_no: int
    status: ExternalReportStatus
    endpoint: str
    payload: VulnerabilityReport
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
