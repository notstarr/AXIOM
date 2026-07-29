from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

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
def list_todos(
    task_id: str,
    response: Response,
    search: str = "",
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[Todo]:
    if limit is None and not search.strip() and status_filter is None:
        items = container.todos.list(task_id)
        response.headers["X-Total-Count"] = str(len(items))
        return items
    items, total = container.todos.list_page(
        task_id,
        search=search,
        status=status_filter,
        limit=limit or 200,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.patch("/{todo_id}", response_model=Todo)
def update_todo(
    task_id: str,
    todo_id: str,
    body: UpdateTodo,
    container: Container = Depends(get_container),
) -> Todo:
    return container.todos.update(task_id, todo_id, body)
