from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.vulnerabilities.reporting import ExternalReportAttempt, ExternalReportService
from axiom.modules.vulnerabilities.schemas import ReportVulnerability, UpdateVulnerability, Vulnerability
from axiom.modules.vulnerabilities.service import VulnerabilityService


router = APIRouter(tags=["v1-vulnerabilities"])


class ExternalPreviewRequest(BaseModel):
    resolved_project: str | None = None


def get_service(engine=Depends(get_v2_engine)) -> VulnerabilityService:
    return VulnerabilityService(engine)


def get_external_service(request: Request, engine=Depends(get_v2_engine)) -> ExternalReportService:
    return ExternalReportService(engine, client=getattr(request.app.state, "mozzie_client", None))


@router.get("/vulnerabilities", response_model=list[Vulnerability])
def list_all_vulnerabilities(
    project_id: str | None = None,
    task_id: str | None = None,
    search: str = "",
    status: str | None = None,
    severity: str | None = None,
    include_archived: bool = False,
    service: VulnerabilityService = Depends(get_service),
) -> list[Vulnerability]:
    return service.list_all(
        project_id=project_id,
        task_id=task_id,
        search=search,
        status=status,
        severity=severity,
        include_archived=include_archived,
    )


@router.post(
    "/internal/tasks/{task_id}/vulnerabilities",
    response_model=Vulnerability,
    status_code=201,
    dependencies=[Depends(require_service_token)],
)
def report_vulnerability(
    task_id: str,
    body: ReportVulnerability,
    service: VulnerabilityService = Depends(get_service),
    external: ExternalReportService = Depends(get_external_service),
) -> Vulnerability:
    try:
        vulnerability = service.report(task_id, body)
        with external.engine.connect() as conn:
            task = external.tasks.get(conn, task_id)
        if task and task.external_report_enabled:
            external.report(task_id, vulnerability.id)
        return vulnerability
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.post(
    "/tasks/{task_id}/vulnerabilities",
    response_model=Vulnerability,
    status_code=201,
)
def report_vulnerability_from_ui(
    task_id: str,
    body: ReportVulnerability,
    service: VulnerabilityService = Depends(get_service),
    external: ExternalReportService = Depends(get_external_service),
) -> Vulnerability:
    try:
        vulnerability = service.report(task_id, body)
        with external.engine.connect() as conn:
            task = external.tasks.get(conn, task_id)
        if task and task.external_report_enabled:
            external.report(task_id, vulnerability.id)
        return vulnerability
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.get("/tasks/{task_id}/vulnerabilities", response_model=list[Vulnerability])
def list_vulnerabilities(
    task_id: str,
    service: VulnerabilityService = Depends(get_service),
) -> list[Vulnerability]:
    return service.list(task_id)


@router.patch(
    "/tasks/{task_id}/vulnerabilities/{vuln_id}",
    response_model=Vulnerability,
)
def update_vulnerability(
    task_id: str,
    vuln_id: str,
    body: UpdateVulnerability,
    service: VulnerabilityService = Depends(get_service),
) -> Vulnerability:
    try:
        return service.update(task_id, vuln_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.delete("/tasks/{task_id}/vulnerabilities/{vuln_id}", status_code=204)
def delete_vulnerability(
    task_id: str,
    vuln_id: str,
    service: VulnerabilityService = Depends(get_service),
) -> None:
    try:
        service.archive(task_id, vuln_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.get(
    "/tasks/{task_id}/vulnerabilities/{vuln_id}/export",
    response_class=PlainTextResponse,
)
def export_vulnerability(
    task_id: str,
    vuln_id: str,
    service: VulnerabilityService = Depends(get_service),
) -> str:
    vulnerability = next((item for item in service.list(task_id) if item.id == vuln_id), None)
    if vulnerability is None:
        raise HTTPException(404, "Vulnerability not found")
    return _vulnerability_markdown(vulnerability)


@router.get("/tasks/{task_id}/vulnerabilities-export", response_class=PlainTextResponse)
def export_task_vulnerabilities(
    task_id: str,
    service: VulnerabilityService = Depends(get_service),
) -> str:
    items = service.list(task_id)
    sections = [f"# AXIOM Vulnerability Report\n\nTask: `{task_id}`"]
    sections.extend(_vulnerability_markdown(item, heading=2) for item in items)
    return "\n\n---\n\n".join(sections) + "\n"


@router.post("/tasks/{task_id}/vulnerabilities/{vuln_id}/external-preview")
def external_preview(
    task_id: str,
    vuln_id: str,
    body: ExternalPreviewRequest,
    service: ExternalReportService = Depends(get_external_service),
):
    try:
        return service.preview(task_id, vuln_id, resolved_project=body.resolved_project)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/tasks/{task_id}/vulnerabilities/{vuln_id}/external-report",
    response_model=ExternalReportAttempt,
    status_code=201,
)
def external_report(
    task_id: str,
    vuln_id: str,
    body: ExternalPreviewRequest,
    service: ExternalReportService = Depends(get_external_service),
) -> ExternalReportAttempt:
    try:
        return service.report(task_id, vuln_id, resolved_project=body.resolved_project)
    except ValueError as exc:
        raise HTTPException(404 if "not found:" in str(exc).lower() else 409, str(exc)) from exc


@router.get(
    "/tasks/{task_id}/external-report-attempts",
    response_model=list[ExternalReportAttempt],
)
def list_external_reports(
    task_id: str,
    vulnerability_id: str | None = None,
    service: ExternalReportService = Depends(get_external_service),
) -> list[ExternalReportAttempt]:
    return service.list(task_id, vulnerability_id)


@router.post(
    "/external-report-attempts/{attempt_id}/retry",
    response_model=ExternalReportAttempt,
)
def retry_external_report(
    attempt_id: str,
    service: ExternalReportService = Depends(get_external_service),
) -> ExternalReportAttempt:
    try:
        return service.retry(attempt_id)
    except ValueError as exc:
        raise HTTPException(404 if "not found:" in str(exc).lower() else 409, str(exc)) from exc


@router.post(
    "/internal/external-report-attempts/retry-failed",
    response_model=list[ExternalReportAttempt],
    dependencies=[Depends(require_service_token)],
)
def retry_failed_external_reports(
    limit: int = 1,
    service: ExternalReportService = Depends(get_external_service),
) -> list[ExternalReportAttempt]:
    return service.retry_failed(limit=limit)


def _vulnerability_markdown(vulnerability: Vulnerability, *, heading: int = 1) -> str:
    prefix = "#" * heading
    steps = "\n".join(
        f"{index}. {step}" for index, step in enumerate(vulnerability.reproduction_steps, start=1)
    ) or "-"
    evidence = "\n".join(f"- {item}" for item in vulnerability.evidence) or "-"
    reason = (
        f"\n\n**Status reason:** {vulnerability.status_reason}"
        if vulnerability.status_reason
        else ""
    )
    return f"""{prefix} {vulnerability.title}

- ID: `{vulnerability.id}`
- Severity: `{vulnerability.severity}`
- Status: `{vulnerability.status}`
- Type: `{vulnerability.type}`
- Target: `{vulnerability.affected_target or '-'}`{reason}

## Description

{vulnerability.description}

## Prerequisites

{vulnerability.prerequisites or '-'}

## Impact

{vulnerability.impact}

## Reproduction

{steps}

## Evidence

{evidence}

## Remediation

{vulnerability.remediation or '-'}

## cURL

```bash
{vulnerability.curl_command or '# not provided'}
```
""".strip()
