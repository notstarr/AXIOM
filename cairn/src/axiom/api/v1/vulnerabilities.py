from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.vulnerabilities.reporting import ExternalReportAttempt, ExternalReportService
from axiom.modules.vulnerabilities.schemas import ReportVulnerability, Vulnerability
from axiom.modules.vulnerabilities.service import VulnerabilityService


router = APIRouter(tags=["v1-vulnerabilities"])


class ExternalPreviewRequest(BaseModel):
    resolved_project: str | None = None


def get_service(engine=Depends(get_v2_engine)) -> VulnerabilityService:
    return VulnerabilityService(engine)


def get_external_service(request: Request, engine=Depends(get_v2_engine)) -> ExternalReportService:
    return ExternalReportService(engine, client=getattr(request.app.state, "mozzie_client", None))


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
