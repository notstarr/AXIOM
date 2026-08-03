from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.token_usage.schemas import TokenRange


router = APIRouter(prefix="/tasks", tags=["diagnostics"])


@router.get("/{task_id}/diagnostics")
def task_diagnostics(
    task_id: str,
    token_range: TokenRange = Query(default="all"),
    model: str = Query(default="all", max_length=300),
    container: Container = Depends(get_container),
) -> dict:
    return container.diagnostics.task(task_id, token_range=token_range, model=model)
