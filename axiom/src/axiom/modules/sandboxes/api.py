from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.sandboxes.schemas import TaskSandbox


router = APIRouter(prefix="/tasks", tags=["sandboxes"])


@router.get("/{task_id}/sandbox", response_model=TaskSandbox | None)
def get_task_sandbox(task_id: str, container: Container = Depends(get_container)) -> TaskSandbox | None:
    return container.sandboxes.get(task_id)
