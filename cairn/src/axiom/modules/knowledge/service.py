from __future__ import annotations

from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import project_knowledge, task_hints
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.common.time import utcnow
from axiom.modules.knowledge.schemas import CreateKnowledge, CreateTaskHint, Knowledge, TaskHint
from axiom.modules.projects.repository import ProjectRepository
from axiom.modules.tasks.repository import TaskRepository


class KnowledgeService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.projects = ProjectRepository()
        self.tasks = TaskRepository()

    def add_project_knowledge(self, project_id: str, body: CreateKnowledge) -> Knowledge:
        now = utcnow()
        with self.engine.begin() as conn:
            if not self.projects.exists(conn, project_id):
                raise ValueError(f"Project not found: {project_id}")
            knowledge_id = allocate_readable_id(
                conn, scope=project_id, kind="knowledge", prefix="k"
            )
            values = {
                "id": knowledge_id,
                "project_id": project_id,
                "title": body.title,
                "content": body.content,
                "hint": body.hint,
                "created_by": body.actor,
                "updated_by": body.actor,
                "created_at": now,
                "updated_at": now,
                "archived": False,
            }
            conn.execute(project_knowledge.insert().values(**values))
            return Knowledge(**values)

    def list_project_knowledge(self, project_id: str) -> list[Knowledge]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(project_knowledge)
                .where(
                    project_knowledge.c.project_id == project_id,
                    project_knowledge.c.archived.is_(False),
                )
                .order_by(project_knowledge.c.created_at, project_knowledge.c.id)
            ).mappings()
            return [Knowledge(**row) for row in rows]

    def add_task_hint(self, task_id: str, body: CreateTaskHint) -> TaskHint:
        now = utcnow()
        with self.engine.begin() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.legacy_readonly:
                raise ValueError("Legacy task is read-only")
            hint_id = allocate_readable_id(conn, scope=task_id, kind="hint", prefix="h")
            values = {
                "id": hint_id,
                "task_id": task_id,
                "content": body.content,
                "creator": body.actor,
                "created_at": now,
                "archived": False,
            }
            conn.execute(task_hints.insert().values(**values))
            return TaskHint(**values)

    def list_task_hints(self, task_id: str) -> list[TaskHint]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(task_hints)
                .where(task_hints.c.task_id == task_id, task_hints.c.archived.is_(False))
                .order_by(task_hints.c.created_at, task_hints.c.id)
            ).mappings()
            return [TaskHint(**row) for row in rows]
