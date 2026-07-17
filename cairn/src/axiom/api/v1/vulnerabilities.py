from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.vulnerabilities.external import MozzieAdapter
from axiom.modules.vulnerabilities.schemas import ReportVulnerability, Vulnerability
from axiom.modules.vulnerabilities.service import VulnerabilityService


router = APIRouter(tags=["v1-vulnerabilities"])


class ExternalPreviewRequest(BaseModel):
    resolved_project: str = "others"


def get_service(engine=Depends(get_v2_engine)) -> VulnerabilityService:
    return VulnerabilityService(engine)


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
) -> Vulnerability:
    try:
        return service.report(task_id, body)
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
    service: VulnerabilityService = Depends(get_service),
):
    try:
        vuln = service.get(task_id, vuln_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return MozzieAdapter().build_payload(vuln, resolved_project=body.resolved_project)
