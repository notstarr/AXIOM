from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.vulnerabilities.schemas import Vulnerability, VulnerabilityReport, UpdateVulnerability


router = APIRouter(tags=["vulnerabilities"])


@router.post("/tasks/{task_id}/vulnerabilities", response_model=Vulnerability, status_code=status.HTTP_201_CREATED)
def report_vulnerability(
    task_id: str,
    body: VulnerabilityReport,
    container: Container = Depends(get_container),
) -> Vulnerability:
    return container.vulnerabilities.report(task_id, body)


@router.get("/tasks/{task_id}/vulnerabilities", response_model=list[Vulnerability])
def list_task_vulnerabilities(
    task_id: str,
    container: Container = Depends(get_container),
) -> list[Vulnerability]:
    return container.vulnerabilities.list(task_id)


@router.get("/tasks/{task_id}/vulnerabilities/{vulnerability_id}", response_model=Vulnerability)
def get_vulnerability(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> Vulnerability:
    return container.vulnerabilities.get(task_id, vulnerability_id)


@router.patch("/tasks/{task_id}/vulnerabilities/{vulnerability_id}", response_model=Vulnerability)
def update_vulnerability(
    task_id: str,
    vulnerability_id: str,
    body: UpdateVulnerability,
    container: Container = Depends(get_container),
) -> Vulnerability:
    return container.vulnerabilities.update(task_id, vulnerability_id, body)


@router.delete("/tasks/{task_id}/vulnerabilities/{vulnerability_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_vulnerability(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.vulnerabilities.archive(task_id, vulnerability_id)


@router.get("/vulnerabilities", response_model=list[Vulnerability])
def list_vulnerabilities(
    project_id: str | None = None,
    task_id: str | None = None,
    search: str = "",
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
    include_archived: bool = False,
    container: Container = Depends(get_container),
) -> list[Vulnerability]:
    return container.vulnerabilities.list_all(
        project_id=project_id,
        task_id=task_id,
        search=search,
        status=status_filter,
        severity=severity,
        include_archived=include_archived,
    )
