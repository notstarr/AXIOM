from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.knowledge.repository import KnowledgeRepository
from axiom.modules.knowledge.schemas import CreateKnowledge, Knowledge, UpdateKnowledge
from axiom.modules.projects.repository import ProjectRepository
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


class KnowledgeService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = KnowledgeRepository()
        self.projects = ProjectRepository()

    def create(self, project_id: str, command: CreateKnowledge) -> Knowledge:
        with self.engine.begin() as conn:
            project = self.projects.get(conn, project_id)
            if project is None:
                raise NotFoundError(f"Project not found: {project_id}")
            if project.status != "active":
                raise ConflictError("Knowledge cannot be added to an archived Project")
            return self.repository.create(conn, project_id=project_id, command=command, now=utcnow())

    def list(self, project_id: str, *, include_archived: bool = False) -> list[Knowledge]:
        with self.engine.connect() as conn:
            if self.projects.get(conn, project_id) is None:
                raise NotFoundError(f"Project not found: {project_id}")
            return self.repository.list(conn, project_id, include_archived=include_archived)

    def update(self, project_id: str, knowledge_id: str, command: UpdateKnowledge) -> Knowledge:
        with self.engine.begin() as conn:
            if self.projects.get(conn, project_id) is None:
                raise NotFoundError(f"Project not found: {project_id}")
            item = self.repository.update(
                conn,
                project_id=project_id,
                knowledge_id=knowledge_id,
                command=command,
                now=utcnow(),
            )
        if item is None:
            raise NotFoundError(f"Knowledge not found: {knowledge_id}")
        return item

    def delete(self, project_id: str, knowledge_id: str) -> None:
        with self.engine.begin() as conn:
            if self.projects.get(conn, project_id) is None:
                raise NotFoundError(f"Project not found: {project_id}")
            if not self.repository.delete(conn, project_id, knowledge_id):
                raise NotFoundError(f"Knowledge not found: {knowledge_id}")
