from __future__ import annotations

from fastapi import APIRouter, Depends, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.todos.schemas import CreateTodo, Todo, UpdateTodo


router = APIRouter(prefix="/tasks/{task_id}/todos", tags=["todos"])


@router.post("", response_model=Todo, status_code=status.HTTP_201_CREATED)
def create_todo(
    task_id: str,
    body: CreateTodo,
    container: Container = Depends(get_container),
) -> Todo:
    return container.todos.create(task_id, body)


@router.get("", response_model=list[Todo])
def list_todos(task_id: str, container: Container = Depends(get_container)) -> list[Todo]:
    return container.todos.list(task_id)


@router.patch("/{todo_id}", response_model=Todo)
def update_todo(
    task_id: str,
    todo_id: str,
    body: UpdateTodo,
    container: Container = Depends(get_container),
) -> Todo:
    return container.todos.update(task_id, todo_id, body)
