from __future__ import annotations

from sqlalchemy import Connection, delete, func, or_, select, update

from axiom.infrastructure.database.metadata import (
    credentials,
    project_knowledge,
    projects,
    tasks,
    vulnerabilities,
)
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.projects.schemas import CreateProject, Project, UpdateProject


class ProjectRepository:
    def create(self, conn: Connection, command: CreateProject, *, now: str) -> Project:
        project_id = allocate_readable_id(
            conn,
            scope="global",
            kind="project",
            prefix="prj_",
        )
        values = {
            "id": project_id,
            "title": command.title,
            "description": command.description,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(projects.insert().values(**values))
        return Project(**values)

    def exists(self, conn: Connection, project_id: str) -> bool:
        return (
            conn.execute(select(projects.c.id).where(projects.c.id == project_id)).scalar_one_or_none()
            is not None
        )

    def get(self, conn: Connection, project_id: str) -> Project | None:
        row = conn.execute(
            self._summary_query().where(projects.c.id == project_id)
        ).mappings().one_or_none()
        return Project(**row) if row is not None else None

    def list(
        self,
        conn: Connection,
        *,
        search: str = "",
        status: str | None = None,
    ) -> list[Project]:
        query = self._summary_query()
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    projects.c.id.ilike(pattern),
                    projects.c.title.ilike(pattern),
                    projects.c.description.ilike(pattern),
                )
            )
        if status:
            query = query.where(projects.c.status == status)
        rows = conn.execute(query.order_by(projects.c.created_at, projects.c.id)).mappings()
        return [Project(**row) for row in rows]

    def update(self, conn: Connection, project_id: str, command: UpdateProject, *, now: str) -> Project | None:
        values = command.model_dump(exclude_none=True)
        if not values:
            return self.get(conn, project_id)
        values["updated_at"] = now
        result = conn.execute(update(projects).where(projects.c.id == project_id).values(**values))
        return self.get(conn, project_id) if result.rowcount else None

    def delete(self, conn: Connection, project_id: str) -> bool:
        return bool(conn.execute(delete(projects).where(projects.c.id == project_id)).rowcount)

    @staticmethod
    def _summary_query():
        task_count = (
            select(func.count(tasks.c.id))
            .where(tasks.c.project_id == projects.c.id)
            .correlate(projects)
            .scalar_subquery()
        )
        active_task_count = (
            select(func.count(tasks.c.id))
            .where(tasks.c.project_id == projects.c.id, tasks.c.status == "running")
            .correlate(projects)
            .scalar_subquery()
        )
        vulnerability_count = (
            select(func.count(vulnerabilities.c.id))
            .select_from(vulnerabilities.join(tasks, tasks.c.id == vulnerabilities.c.task_id))
            .where(
                tasks.c.project_id == projects.c.id,
                vulnerabilities.c.archived.is_(False),
            )
            .correlate(projects)
            .scalar_subquery()
        )
        knowledge_count = (
            select(func.count(project_knowledge.c.id))
            .where(
                project_knowledge.c.project_id == projects.c.id,
                project_knowledge.c.archived.is_(False),
            )
            .correlate(projects)
            .scalar_subquery()
        )
        credential_count = (
            select(func.count(credentials.c.id))
            .where(credentials.c.project_id == projects.c.id)
            .correlate(projects)
            .scalar_subquery()
        )
        return select(
            projects,
            task_count.label("task_count"),
            active_task_count.label("active_task_count"),
            vulnerability_count.label("vulnerability_count"),
            knowledge_count.label("knowledge_count"),
            credential_count.label("credential_count"),
        )
