from __future__ import annotations

from sqlalchemy import Engine, func, select

from axiom.modules.credentials.tables import credentials
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.projects.schemas import CreateProject, Project, UpdateProject
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


class ProjectService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = ProjectRepository()

    def create(self, command: CreateProject) -> Project:
        with self.engine.begin() as conn:
            return self.repository.create(conn, command, now=utcnow())

    def get(self, project_id: str) -> Project:
        with self.engine.connect() as conn:
            project = self.repository.get(conn, project_id)
        if project is None:
            raise NotFoundError(f"Project not found: {project_id}")
        return self._decorate(project)

    def list(self, *, search: str = "", status: str | None = None) -> list[Project]:
        with self.engine.connect() as conn:
            projects = self.repository.list(conn, search=search, status=status)
        return [self._decorate(project) for project in projects]

    def update(self, project_id: str, command: UpdateProject) -> Project:
        with self.engine.begin() as conn:
            project = self.repository.update(conn, project_id, command, now=utcnow())
        if project is None:
            raise NotFoundError(f"Project not found: {project_id}")
        return self._decorate(project)

    def delete(self, project_id: str) -> None:
        with self.engine.begin() as conn:
            project = self.repository.get(conn, project_id)
            if project is None:
                raise NotFoundError(f"Project not found: {project_id}")
            task_count = conn.execute(
                select(func.count()).select_from(tasks).where(tasks.c.project_id == project_id)
            ).scalar_one()
            if task_count:
                raise ConflictError("Project with Tasks must be archived instead of deleted")
            self.repository.delete(conn, project_id)

    def _decorate(self, project: Project) -> Project:
        with self.engine.connect() as conn:
            task_count = conn.execute(
                select(func.count()).select_from(tasks).where(tasks.c.project_id == project.id)
            ).scalar_one()
            active_task_count = conn.execute(
                select(func.count()).select_from(tasks).where(
                    tasks.c.project_id == project.id,
                    tasks.c.status == "running",
                    tasks.c.archived.is_(False),
                )
            ).scalar_one()
            vulnerability_count = conn.execute(
                select(func.count())
                .select_from(vulnerabilities)
                .join(tasks, tasks.c.id == vulnerabilities.c.task_id)
                .where(tasks.c.project_id == project.id, vulnerabilities.c.archived.is_(False))
            ).scalar_one()
            knowledge_count = conn.execute(
                select(func.count()).select_from(project_knowledge).where(
                    project_knowledge.c.project_id == project.id,
                    project_knowledge.c.archived.is_(False),
                )
            ).scalar_one()
            credential_count = conn.execute(
                select(func.count()).select_from(credentials).where(
                    credentials.c.project_id == project.id,
                    credentials.c.enabled.is_(True),
                )
            ).scalar_one()
        return project.model_copy(
            update={
                "task_count": task_count,
                "active_task_count": active_task_count,
                "vulnerability_count": vulnerability_count,
                "knowledge_count": knowledge_count,
                "credential_count": credential_count,
            }
        )
