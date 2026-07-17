from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_task_service
from axiom.modules.tasks.schemas import CreateTask, Task, TaskScope
from axiom.modules.tasks.service import TaskService


router = APIRouter(prefix="/tasks", tags=["v1-tasks"])


@router.post("", response_model=Task, status_code=201)
def create_task(
    body: CreateTask,
    service: TaskService = Depends(get_task_service),
) -> Task:
    try:
        return service.create(body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if detail.startswith("Project not found:") else 422, detail) from exc


@router.get("", response_model=list[Task])
def list_tasks(service: TaskService = Depends(get_task_service)) -> list[Task]:
    return service.list()


@router.get("/{task_id}", response_model=Task)
def get_task(
    task_id: str,
    service: TaskService = Depends(get_task_service),
) -> Task:
    task = service.get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.patch("/{task_id}/scope", response_model=Task)
def update_task_scope(
    task_id: str,
    body: TaskScope,
    service: TaskService = Depends(get_task_service),
) -> Task:
    try:
        return service.update_scope(task_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc
