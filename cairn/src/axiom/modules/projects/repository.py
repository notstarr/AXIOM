from __future__ import annotations

from sqlalchemy import Connection, select

from axiom.infrastructure.database.metadata import projects
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.projects.schemas import CreateProject, Project


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
        row = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().one_or_none()
        return Project(**row) if row is not None else None

    def list(self, conn: Connection) -> list[Project]:
        rows = conn.execute(select(projects).order_by(projects.c.created_at, projects.c.id)).mappings()
        return [Project(**row) for row in rows]
