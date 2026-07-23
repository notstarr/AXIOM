from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.tasks.schemas import (
    CreateTask,
    CreateTaskHint,
    Task,
    TaskHint,
    UpdateTask,
    UpdateTaskHint,
)


router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=Task, status_code=status.HTTP_201_CREATED)
def create_task(body: CreateTask, container: Container = Depends(get_container)) -> Task:
    return container.tasks.create(body)


@router.get("", response_model=list[Task])
def list_tasks(
    project_id: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    search: str = "",
    container: Container = Depends(get_container),
) -> list[Task]:
    return container.tasks.list(
        project_id=project_id,
        status=status,
        phase=phase,
        search=search,
    )


@router.get("/{task_id}", response_model=Task)
def get_task(task_id: str, container: Container = Depends(get_container)) -> Task:
    return container.tasks.get(task_id)


@router.patch("/{task_id}", response_model=Task)
def update_task(
    task_id: str,
    body: UpdateTask,
    container: Container = Depends(get_container),
) -> Task:
    return container.tasks.update(task_id, body)


@router.get("/{task_id}/hints", response_model=list[TaskHint])
def list_task_hints(task_id: str, container: Container = Depends(get_container)) -> list[TaskHint]:
    return container.tasks.hints(task_id)


@router.post("/{task_id}/hints", response_model=TaskHint, status_code=status.HTTP_201_CREATED)
def add_task_hint(
    task_id: str,
    body: CreateTaskHint,
    container: Container = Depends(get_container),
) -> TaskHint:
    return container.tasks.add_hint(task_id, body)


@router.patch("/{task_id}/hints/{hint_id}", response_model=TaskHint)
def update_task_hint(
    task_id: str,
    hint_id: str,
    body: UpdateTaskHint,
    container: Container = Depends(get_container),
) -> TaskHint:
    return container.tasks.update_hint(task_id, hint_id, body)


@router.post("/{task_id}/stop", response_model=Task)
def stop_task(task_id: str, container: Container = Depends(get_container)) -> Task:
    return container.tasks.stop(task_id)


@router.post("/{task_id}/resume", response_model=Task)
def resume_task(task_id: str, container: Container = Depends(get_container)) -> Task:
    return container.tasks.resume(task_id)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: str, container: Container = Depends(get_container)) -> Response:
    container.tasks.delete(task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
