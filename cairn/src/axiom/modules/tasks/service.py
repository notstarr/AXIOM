from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.common.time import utcnow
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import CreateTask, Task, TaskScope
from sqlalchemy import update
from axiom.infrastructure.database.metadata import tasks


class TaskService:
    def __init__(
        self,
        engine: Engine,
        repository: TaskRepository | None = None,
        project_repository: ProjectRepository | None = None,
    ):
        self.engine = engine
        self.repository = repository or TaskRepository()
        self.project_repository = project_repository or ProjectRepository()

    def create(self, command: CreateTask) -> Task:
        origin = parse_origin(command.origin)
        with self.engine.begin() as conn:
            if not self.project_repository.exists(conn, command.project_id):
                raise ValueError(f"Project not found: {command.project_id}")
            return self.repository.create(conn, command, origin=origin, now=utcnow())

    def get(self, task_id: str) -> Task | None:
        with self.engine.connect() as conn:
            return self.repository.get(conn, task_id)

    def list(self) -> list[Task]:
        with self.engine.connect() as conn:
            return self.repository.list(conn)

    def update_scope(self, task_id: str, scope: TaskScope) -> Task:
        with self.engine.begin() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            normalized = scope
            if scope.enabled and not scope.targets:
                normalized = scope.model_copy(update={"targets": [task.origin.host]})
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    scope_enabled=normalized.enabled,
                    scope_json=normalized.model_dump_json(),
                    updated_at=utcnow(),
                )
            )
            return self.repository.get(conn, task_id)  # type: ignore[return-value]
