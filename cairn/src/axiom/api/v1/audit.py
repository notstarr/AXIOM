from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.audit.schemas import AuditEvent, RecordAuditEvent
from axiom.modules.audit.service import AuditService


router = APIRouter(tags=["v1-audit"])
internal = APIRouter(
    prefix="/internal",
    tags=["v1-internal-audit"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/tasks/{task_id}/audit-events", response_model=list[AuditEvent])
def list_task_events(task_id: str, limit: int = 500, engine=Depends(get_v2_engine)):
    return AuditService(engine).list(task_id=task_id, limit=limit)


@router.get("/projects/{project_id}/audit-events", response_model=list[AuditEvent])
def list_project_events(project_id: str, limit: int = 500, engine=Depends(get_v2_engine)):
    return AuditService(engine).list(project_id=project_id, limit=limit)


@internal.post("/audit-events", response_model=AuditEvent, status_code=201)
def record_event(body: RecordAuditEvent, engine=Depends(get_v2_engine)):
    return AuditService(engine).record(body)
