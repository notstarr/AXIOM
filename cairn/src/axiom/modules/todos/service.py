from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.common.time import utcnow
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import CreateTodo, Todo, TodoStatus


class TodoService:
    def __init__(
        self,
        engine: Engine,
        repository: TodoRepository | None = None,
        task_repository: TaskRepository | None = None,
    ):
        self.engine = engine
        self.repository = repository or TodoRepository()
        self.task_repository = task_repository or TaskRepository()

    def create(self, task_id: str, command: CreateTodo) -> Todo:
        if not command.sources:
            raise ValueError("Todo requires at least one source")
        with self.engine.begin() as conn:
            task = self.task_repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            for source in command.sources:
                if not self.repository.source_exists(
                    conn,
                    task_id=task_id,
                    project_id=task.project_id,
                    source=source,
                ):
                    raise ValueError(f"Todo source not found: {source.type}:{source.id}")
            return self.repository.create(conn, task_id, command, now=utcnow())

    def list(self, task_id: str) -> list[Todo]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, task_id)

    def update(
        self,
        task_id: str,
        todo_id: str,
        *,
        actor: str,
        title: str | None = None,
        category: str | None = None,
        theme: str | None = None,
        priority: int | None = None,
        status: TodoStatus | None = None,
        status_reason: str | None = None,
    ) -> Todo:
        if not actor.strip():
            raise ValueError("actor must not be empty")
        now = utcnow()
        values = {"updated_at": now}
        for key, value in {
            "title": title,
            "category": category,
            "theme": theme,
            "priority": priority,
            "status": status,
            "status_reason": status_reason,
        }.items():
            if value is not None:
                values[key] = value
        if status == "completed":
            values["completed_at"] = now
        elif status is not None:
            values["completed_at"] = None

        with self.engine.begin() as conn:
            task = self.task_repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            todo = self.repository.update(conn, task_id, todo_id, values)
            if todo is None:
                raise ValueError(f"Todo not found: {todo_id}")
            return todo
