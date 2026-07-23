from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.external_reports.schemas import (
    ExternalReportAttempt,
    ExternalReportConfiguration,
    ExternalReportPreview,
    ExternalReportResult,
)


router = APIRouter(prefix="/tasks", tags=["external-reports"])


@router.get("/{task_id}/external-report/configuration", response_model=ExternalReportConfiguration)
def external_report_configuration(
    task_id: str,
    container: Container = Depends(get_container),
) -> ExternalReportConfiguration:
    return container.external_reports.configuration(task_id)


@router.post(
    "/{task_id}/vulnerabilities/{vulnerability_id}/external-preview",
    response_model=ExternalReportPreview,
)
def preview_external_report(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> ExternalReportPreview:
    return container.external_reports.preview(task_id, vulnerability_id)


@router.get(
    "/{task_id}/vulnerabilities/{vulnerability_id}/external-attempts",
    response_model=list[ExternalReportAttempt],
)
def list_external_report_attempts(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> list[ExternalReportAttempt]:
    return container.external_reports.attempts(task_id, vulnerability_id)


@router.post(
    "/{task_id}/vulnerabilities/{vulnerability_id}/external-report",
    response_model=ExternalReportResult,
)
def submit_external_report(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> ExternalReportResult:
    return container.external_reports.submit(task_id, vulnerability_id)


@router.post(
    "/{task_id}/vulnerabilities/{vulnerability_id}/external-report/retry",
    response_model=ExternalReportResult,
)
def retry_external_report(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> ExternalReportResult:
    return container.external_reports.retry(task_id, vulnerability_id)
