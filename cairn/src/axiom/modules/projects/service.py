from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.common.time import utcnow
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.projects.schemas import CreateProject, Project


class ProjectService:
    def __init__(self, engine: Engine, repository: ProjectRepository | None = None):
        self.engine = engine
        self.repository = repository or ProjectRepository()

    def create(self, command: CreateProject) -> Project:
        with self.engine.begin() as conn:
            return self.repository.create(conn, command, now=utcnow())

    def get(self, project_id: str) -> Project | None:
        with self.engine.connect() as conn:
            return self.repository.get(conn, project_id)

    def list(self) -> list[Project]:
        with self.engine.connect() as conn:
            return self.repository.list(conn)
