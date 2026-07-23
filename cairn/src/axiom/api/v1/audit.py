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


@router.get("/audit-events", response_model=list[AuditEvent])
def list_events(
    project_id: str | None = None,
    task_id: str | None = None,
    intent_id: str | None = None,
    event_type: str | None = None,
    level: str | None = None,
    search: str = "",
    after_id: str | None = None,
    limit: int = 500,
    engine=Depends(get_v2_engine),
):
    return AuditService(engine).list(
        project_id=project_id,
        task_id=task_id,
        intent_id=intent_id,
        event_type=event_type,
        level=level,
        search=search,
        after_id=after_id,
        limit=limit,
    )


@router.get("/tasks/{task_id}/audit-events", response_model=list[AuditEvent])
def list_task_events(
    task_id: str,
    after_id: str | None = None,
    limit: int = 500,
    engine=Depends(get_v2_engine),
):
    return AuditService(engine).list(task_id=task_id, after_id=after_id, limit=limit)


@router.get("/projects/{project_id}/audit-events", response_model=list[AuditEvent])
def list_project_events(
    project_id: str,
    after_id: str | None = None,
    limit: int = 500,
    engine=Depends(get_v2_engine),
):
    return AuditService(engine).list(project_id=project_id, after_id=after_id, limit=limit)


@internal.post("/audit-events", response_model=AuditEvent, status_code=201)
def record_event(body: RecordAuditEvent, engine=Depends(get_v2_engine)):
    return AuditService(engine).record(body)
