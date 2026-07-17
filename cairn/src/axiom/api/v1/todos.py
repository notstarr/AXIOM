from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_v2_engine
from axiom.modules.todos.schemas import CreateTodo, Todo, UpdateTodo
from axiom.modules.todos.service import TodoService


router = APIRouter(prefix="/tasks/{task_id}/todos", tags=["v1-todos"])


def get_todo_service(engine=Depends(get_v2_engine)) -> TodoService:
    return TodoService(engine)


@router.post("", response_model=Todo, status_code=201)
def create_todo(
    task_id: str,
    body: CreateTodo,
    service: TodoService = Depends(get_todo_service),
) -> Todo:
    try:
        return service.create(task_id, body)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found:" in detail.lower() else 409 if "read-only" in detail else 422
        raise HTTPException(status, detail) from exc


@router.get("", response_model=list[Todo])
def list_todos(
    task_id: str,
    service: TodoService = Depends(get_todo_service),
) -> list[Todo]:
    return service.list(task_id)


@router.patch("/{todo_id}", response_model=Todo)
def update_todo(
    task_id: str,
    todo_id: str,
    body: UpdateTodo,
    service: TodoService = Depends(get_todo_service),
) -> Todo:
    try:
        return service.update(
            task_id,
            todo_id,
            actor=body.actor,
            title=body.title,
            category=body.category,
            theme=body.theme,
            priority=body.priority,
            status=body.status,
            status_reason=body.status_reason,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found:" in detail.lower() else 409 if "read-only" in detail else 422
        raise HTTPException(status, detail) from exc
