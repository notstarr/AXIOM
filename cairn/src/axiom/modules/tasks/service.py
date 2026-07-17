from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.common.time import utcnow
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import CreateTask, Task


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
