from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import (
    CreateTodo,
    LEGACY_TODO_STATUS_MAP,
    Todo,
    UpdateTodo,
    TODO_STATUS_REASON_REQUIRED,
)
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


class TodoService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = TodoRepository()
        self.tasks = TaskRepository()

    def create(self, task_id: str, command: CreateTodo) -> Todo:
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if task.archived:
                raise ConflictError("已归档任务不能新增 Todo，请先恢复任务")
            for source in command.sources:
                if not self.repository.source_exists(conn, task_id=task_id, source=source):
                    raise ConflictError(f"Todo source not found: {source.type}:{source.id}")
            return self.repository.create(conn, task_id=task_id, command=command, now=utcnow())

    def list(self, task_id: str) -> list[Todo]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的 Todo 不可见，请先恢复任务")
            return self.repository.list(conn, task_id)

    def list_page(
        self,
        task_id: str,
        *,
        search: str = "",
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Todo], int]:
        if status is not None:
            normalized_status = status.strip()
            status = LEGACY_TODO_STATUS_MAP.get(normalized_status, normalized_status)
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.archived:
                raise ConflictError("已归档任务的 Todo 不可见，请先恢复任务")
            return self.repository.list_page(
                conn,
                task_id,
                search=search,
                status=status,
                limit=limit,
                offset=offset,
            )

    def update(self, task_id: str, todo_id: str, command: UpdateTodo) -> Todo:
        now = utcnow()
        values = command.model_dump(exclude_none=True, exclude={"actor", "source_adds"})
        values["updated_at"] = now
        if command.status == "completed":
            values["completed_at"] = now
        elif command.status is not None:
            values["completed_at"] = None
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ConflictError("Legacy Task is read-only")
            if task.archived:
                raise ConflictError("已归档任务不能修改 Todo，请先恢复任务")
            current = self.repository.get(conn, task_id, todo_id)
            if current is None:
                raise NotFoundError(f"Todo not found: {todo_id}")
            if current.archived:
                raise ConflictError(f"Archived Todo cannot be updated: {todo_id}")
            effective_status = command.status or current.status
            effective_reason = (
                command.status_reason
                if command.status_reason is not None
                else current.status_reason
            )
            if effective_status in TODO_STATUS_REASON_REQUIRED and not effective_reason.strip():
                raise ConflictError(
                    f"status_reason is required for Todo status {effective_status}"
                )
            for source in command.source_adds:
                if not self.repository.source_exists(conn, task_id=task_id, source=source):
                    raise ConflictError(f"Todo source not found: {source.type}:{source.id}")
            todo = self.repository.update(conn, task_id, todo_id, values)
            if todo is None:
                raise NotFoundError(f"Todo not found: {todo_id}")
            self.repository.add_sources(
                conn,
                task_id=task_id,
                todo_id=todo_id,
                sources=command.source_adds,
            )
            result = self.repository.get(conn, task_id, todo_id)
            assert result is not None
            return result
