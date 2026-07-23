from __future__ import annotations

from sqlalchemy import Connection, delete, or_, select, update

from axiom.modules.projects.schemas import CreateProject, Project, UpdateProject
from axiom.modules.projects.tables import projects
from axiom.shared.ids import allocate_id


class ProjectRepository:
    def create(self, conn: Connection, command: CreateProject, *, now: str) -> Project:
        values = {
            "id": allocate_id(
                conn,
                scope="global",
                kind="project",
                prefix="prj_",
            ),
            "title": command.title,
            "description": command.description,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(projects.insert().values(**values))
        return Project.model_validate(values)

    def get(self, conn: Connection, project_id: str) -> Project | None:
        row = conn.execute(
            select(projects).where(projects.c.id == project_id)
        ).mappings().one_or_none()
        return Project.model_validate(row) if row is not None else None

    def list(
        self,
        conn: Connection,
        *,
        search: str = "",
        status: str | None = None,
    ) -> list[Project]:
        statement = select(projects)
        if status:
            statement = statement.where(projects.c.status == status)
        if search.strip():
            pattern = f"%{search.strip()}%"
            statement = statement.where(
                or_(projects.c.id.ilike(pattern), projects.c.title.ilike(pattern))
            )
        rows = conn.execute(
            statement.order_by(projects.c.created_at.desc(), projects.c.id.desc())
        ).mappings()
        return [Project.model_validate(row) for row in rows]

    def update(
        self,
        conn: Connection,
        project_id: str,
        command: UpdateProject,
        *,
        now: str,
    ) -> Project | None:
        values = command.model_dump(exclude_none=True)
        if values:
            values["updated_at"] = now
            result = conn.execute(
                update(projects).where(projects.c.id == project_id).values(**values)
            )
            if not result.rowcount:
                return None
        return self.get(conn, project_id)

    def delete(self, conn: Connection, project_id: str) -> bool:
        return bool(
            conn.execute(delete(projects).where(projects.c.id == project_id)).rowcount
        )
