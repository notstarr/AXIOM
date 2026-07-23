from __future__ import annotations

from sqlalchemy import Connection, select, update

from axiom.infrastructure.database.metadata import artifacts
from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.common.ids import allocate_readable_id


class ArtifactRepository:
    def get(self, conn: Connection, task_id: str, artifact_id: str) -> Artifact | None:
        row = conn.execute(
            select(artifacts).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
                artifacts.c.archived.is_(False),
            )
        ).mappings().one_or_none()
        return Artifact(**row) if row is not None else None

    def create(self, conn: Connection, *, task_id: str, now: str, values: dict) -> Artifact:
        artifact_id = allocate_readable_id(
            conn, scope=task_id, kind="artifact", prefix="a"
        )
        row = {"id": artifact_id, "task_id": task_id, "created_at": now, **values}
        conn.execute(artifacts.insert().values(**row))
        return Artifact(**row)

    def list(self, conn: Connection, task_id: str) -> list[Artifact]:
        rows = conn.execute(
            select(artifacts)
            .where(artifacts.c.task_id == task_id, artifacts.c.archived.is_(False))
            .order_by(artifacts.c.created_at, artifacts.c.id)
        ).mappings()
        return [Artifact(**row) for row in rows]

    def upsert_path(
        self,
        conn: Connection,
        *,
        task_id: str,
        path: str,
        now: str,
        values: dict,
    ) -> Artifact:
        row = conn.execute(
            select(artifacts).where(
                artifacts.c.task_id == task_id,
                artifacts.c.path == path,
            )
        ).mappings().one_or_none()
        if row is None:
            return self.create(conn, task_id=task_id, now=now, values=values)
        conn.execute(
            update(artifacts)
            .where(artifacts.c.task_id == task_id, artifacts.c.id == row["id"])
            .values(**values)
        )
        updated = {**dict(row), **values}
        return Artifact(**updated)

    def archive(self, conn: Connection, task_id: str, artifact_id: str) -> Artifact | None:
        artifact = self.get(conn, task_id, artifact_id)
        if artifact is None:
            return None
        conn.execute(
            update(artifacts)
            .where(artifacts.c.task_id == task_id, artifacts.c.id == artifact_id)
            .values(archived=True)
        )
        return artifact
