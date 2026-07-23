from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, delete, or_, select

from axiom.infrastructure.database.metadata import artifacts, audit_events, id_counters, tasks
from axiom.modules.common.time import utcnow
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.projects.schemas import CreateProject, Project, UpdateProject
from axiom.modules.tasks.storage import TaskStorage


class ProjectService:
    def __init__(
        self,
        engine: Engine,
        repository: ProjectRepository | None = None,
        *,
        data_dir: str | Path | None = None,
        storage: TaskStorage | None = None,
    ):
        self.engine = engine
        self.repository = repository or ProjectRepository()
        self.storage = storage or TaskStorage(data_dir)

    def create(self, command: CreateProject) -> Project:
        with self.engine.begin() as conn:
            return self.repository.create(conn, command, now=utcnow())

    def get(self, project_id: str) -> Project | None:
        with self.engine.connect() as conn:
            return self.repository.get(conn, project_id)

    def list(self, *, search: str = "", status: str | None = None) -> list[Project]:
        with self.engine.connect() as conn:
            return self.repository.list(conn, search=search, status=status)

    def update(self, project_id: str, command: UpdateProject) -> Project:
        with self.engine.begin() as conn:
            project = self.repository.update(conn, project_id, command, now=utcnow())
            if project is None:
                raise ValueError(f"Project not found: {project_id}")
            return project

    def delete(self, project_id: str) -> None:
        with self.engine.connect() as conn:
            if not self.repository.exists(conn, project_id):
                raise ValueError(f"Project not found: {project_id}")
            task_ids = set(
                conn.execute(
                    select(tasks.c.id).where(tasks.c.project_id == project_id)
                ).scalars()
            )
            artifact_paths_by_task = {task_id: [] for task_id in task_ids}
            if task_ids:
                rows = conn.execute(
                    select(artifacts.c.task_id, artifacts.c.path).where(
                        artifacts.c.task_id.in_(task_ids)
                    )
                )
                for task_id, path in rows:
                    artifact_paths_by_task[task_id].append(path)

        self.storage.delete_project_data(
            project_id,
            artifact_paths_by_task=artifact_paths_by_task,
        )

        with self.engine.begin() as conn:
            audit_filter = audit_events.c.project_id == project_id
            if task_ids:
                audit_filter = or_(audit_filter, audit_events.c.task_id.in_(task_ids))
                conn.execute(delete(id_counters).where(id_counters.c.scope.in_(task_ids)))
            conn.execute(delete(audit_events).where(audit_filter))
            conn.execute(delete(id_counters).where(id_counters.c.scope == project_id))
            if not self.repository.delete(conn, project_id):
                raise ValueError(f"Project not found: {project_id}")
