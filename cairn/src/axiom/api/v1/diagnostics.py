from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_v2_engine
from axiom.modules.diagnostics.service import TaskDiagnosticsService


router = APIRouter(tags=["v1-diagnostics"])


@router.get("/tasks/{task_id}/diagnostics")
def task_diagnostics(task_id: str, engine=Depends(get_v2_engine)):
    try:
        return TaskDiagnosticsService(engine).get(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
