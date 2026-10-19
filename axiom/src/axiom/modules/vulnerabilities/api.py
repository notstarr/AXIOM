from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.vulnerabilities.schemas import (
    SetVulnerabilityHumanReviewed,
    SetVulnerabilityRedTeamExploitable,
    UpdateVulnerability,
    Vulnerability,
    VulnerabilityReport,
)


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
    response: Response,
    search: str = "",
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
    red_team_exploitable: bool | None = None,
    include_archived: bool = False,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[Vulnerability]:
    if limit is None and not any(
        (search.strip(), status_filter, severity, red_team_exploitable is not None, include_archived)
    ):
        items = container.vulnerabilities.list(task_id)
        response.headers["X-Total-Count"] = str(len(items))
        return items
    effective_limit = limit or 200
    items, total = container.vulnerabilities.list_page(
        task_id=task_id,
        search=search,
        status=status_filter,
        severity=severity,
        red_team_exploitable=red_team_exploitable,
        include_archived=include_archived,
        limit=effective_limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


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


@router.post(
    "/tasks/{task_id}/vulnerabilities/{vulnerability_id}/human-reviewed",
    response_model=Vulnerability,
)
def set_vulnerability_human_reviewed(
    task_id: str,
    vulnerability_id: str,
    body: SetVulnerabilityHumanReviewed,
    container: Container = Depends(get_container),
) -> Vulnerability:
    return container.vulnerabilities.set_human_reviewed(
        task_id,
        vulnerability_id,
        value=body.value,
        actor=body.actor,
    )


@router.post(
    "/tasks/{task_id}/vulnerabilities/{vulnerability_id}/red-team-exploitable",
    response_model=Vulnerability,
)
def set_vulnerability_red_team_exploitable(
    task_id: str,
    vulnerability_id: str,
    body: SetVulnerabilityRedTeamExploitable,
    container: Container = Depends(get_container),
) -> Vulnerability:
    return container.vulnerabilities.set_red_team_exploitable(
        task_id,
        vulnerability_id,
        value=body.value,
        actor=body.actor,
    )


@router.delete("/tasks/{task_id}/vulnerabilities/{vulnerability_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_vulnerability(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.vulnerabilities.archive(task_id, vulnerability_id)


@router.get("/vulnerabilities", response_model=list[Vulnerability])
def list_vulnerabilities(
    response: Response,
    project_id: str | None = None,
    task_id: str | None = None,
    search: str = "",
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
    red_team_exploitable: bool | None = None,
    include_archived: bool = False,
    sort: str = Query(default="updated", pattern="^(updated|severity|unreviewed|newest)$"),
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[Vulnerability]:
    filters = dict(
        project_id=project_id,
        task_id=task_id,
        search=search,
        status=status_filter,
        severity=severity,
        red_team_exploitable=red_team_exploitable,
        include_archived=include_archived,
        sort=sort,
    )
    if limit is None:
        items = container.vulnerabilities.list_all(**filters)
        response.headers["X-Total-Count"] = str(len(items))
        return items
    items, total = container.vulnerabilities.list_page(
        **filters,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items
