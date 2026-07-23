from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, delete, select, update

from axiom.infrastructure.database.metadata import (
    artifacts,
    audit_events,
    id_counters,
    intents,
    tasks,
)
from axiom.modules.common.time import utcnow
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import (
    CreateTask,
    Task,
    TaskScope,
    TaskWorkerPoolUpdate,
    UpdateTask,
    UpdateTaskWorkerPool,
)
from axiom.modules.tasks.storage import TaskStorage


class TaskService:
    def __init__(
        self,
        engine: Engine,
        repository: TaskRepository | None = None,
        project_repository: ProjectRepository | None = None,
        *,
        data_dir: str | Path | None = None,
        storage: TaskStorage | None = None,
    ):
        self.engine = engine
        self.repository = repository or TaskRepository()
        self.project_repository = project_repository or ProjectRepository()
        self.storage = storage or TaskStorage(data_dir)

    def create(self, command: CreateTask) -> Task:
        origin = parse_origin(command.origin)
        with self.engine.begin() as conn:
            if not self.project_repository.exists(conn, command.project_id):
                raise ValueError(f"Project not found: {command.project_id}")
            return self.repository.create(conn, command, origin=origin, now=utcnow())

    def get(self, task_id: str) -> Task | None:
        with self.engine.connect() as conn:
            return self.repository.get(conn, task_id)

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        phase: str | None = None,
        search: str = "",
    ) -> list[Task]:
        with self.engine.connect() as conn:
            return self.repository.list(
                conn,
                project_id=project_id,
                status=status,
                phase=phase,
                search=search,
            )

    def update(self, task_id: str, command: UpdateTask) -> Task:
        with self.engine.begin() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            updated = self.repository.update(conn, task_id, command, now=utcnow())
            assert updated is not None
            return updated

    def delete(self, task_id: str) -> None:
        with self.engine.connect() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            artifact_paths = list(
                conn.execute(
                    select(artifacts.c.path).where(artifacts.c.task_id == task_id)
                ).scalars()
            )

        # Files are part of the Task transaction from the user's point of
        # view.  Refuse the DB deletion if any owned path could not be removed,
        # otherwise the UI would hide a Task while its private data remained.
        self.storage.delete_task_data(
            task_id,
            project_id=task.project_id,
            artifact_paths=artifact_paths,
        )

        with self.engine.begin() as conn:
            current = self.repository.get(conn, task_id)
            if current is None:
                raise ValueError(f"Task not found: {task_id}")
            conn.execute(delete(audit_events).where(audit_events.c.task_id == task_id))
            conn.execute(delete(id_counters).where(id_counters.c.scope == task_id))
            self.repository.delete(conn, task_id)

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

    def update_worker_pool(
        self,
        task_id: str,
        command: UpdateTaskWorkerPool,
    ) -> TaskWorkerPoolUpdate:
        now = utcnow()
        encoded = json.dumps(command.worker_pool, ensure_ascii=False)
        with self.engine.begin() as conn:
            task = self.repository.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            if task.status == "completed":
                raise ValueError("Completed Task cannot change Worker Pool")
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(worker_pool_json=encoded, updated_at=now)
            )
            changed = conn.execute(
                update(intents)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.status == "pending",
                    intents.c.worker.is_(None),
                )
                .values(worker_pool_json=encoded)
            )
            updated = self.repository.get(conn, task_id)
            assert updated is not None
            return TaskWorkerPoolUpdate(
                task=updated,
                updated_unclaimed_intents=max(0, int(changed.rowcount or 0)),
            )
