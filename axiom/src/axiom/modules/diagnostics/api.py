from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container


router = APIRouter(prefix="/tasks", tags=["diagnostics"])


@router.get("/{task_id}/diagnostics")
def task_diagnostics(task_id: str, container: Container = Depends(get_container)) -> dict:
    return container.diagnostics.task(task_id)
