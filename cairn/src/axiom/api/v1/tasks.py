from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from axiom.api.v1.dependencies import get_task_service
from axiom.modules.tasks.schemas import (
    CreateTask,
    Task,
    TaskScope,
    TaskWorkerPoolUpdate,
    UpdateTask,
    UpdateTaskWorkerPool,
)
from axiom.modules.tasks.service import TaskService
from axiom.modules.tasks.export import TaskExportService
from axiom.api.v1.dependencies import get_v2_engine


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
def list_tasks(
    project_id: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    search: str = "",
    service: TaskService = Depends(get_task_service),
) -> list[Task]:
    return service.list(project_id=project_id, status=status, phase=phase, search=search)


@router.get("/{task_id}", response_model=Task)
def get_task(
    task_id: str,
    service: TaskService = Depends(get_task_service),
) -> Task:
    task = service.get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/{task_id}/export", response_class=PlainTextResponse)
def export_task(
    task_id: str,
    format: str = "yaml",
    engine=Depends(get_v2_engine),
) -> str:
    try:
        return TaskExportService(engine).export(task_id, format=format)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 400, detail) from exc


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


@router.patch("/{task_id}/worker-pool", response_model=TaskWorkerPoolUpdate)
def update_task_worker_pool(
    task_id: str,
    body: UpdateTaskWorkerPool,
    service: TaskService = Depends(get_task_service),
) -> TaskWorkerPoolUpdate:
    try:
        return service.update_worker_pool(task_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.patch("/{task_id}", response_model=Task)
def update_task(
    task_id: str,
    body: UpdateTask,
    service: TaskService = Depends(get_task_service),
) -> Task:
    try:
        return service.update(task_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.delete("/{task_id}", status_code=204)
def delete_task(
    task_id: str,
    service: TaskService = Depends(get_task_service),
) -> None:
    try:
        service.delete(task_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc
