from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.audit.schemas import AuditEvent


router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=list[AuditEvent])
def list_audit_events(
    project_id: str | None = None,
    task_id: str | None = None,
    intent_id: str | None = None,
    event_type: str | None = None,
    level: str | None = None,
    search: str = "",
    after_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=2_000),
    container: Container = Depends(get_container),
) -> list[AuditEvent]:
    return container.audit.list(
        project_id=project_id,
        task_id=task_id,
        intent_id=intent_id,
        event_type=event_type,
        level=level,
        search=search,
        after_id=after_id,
        limit=limit,
    )
