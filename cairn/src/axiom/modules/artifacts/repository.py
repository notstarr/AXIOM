from __future__ import annotations

from sqlalchemy import Connection, select

from axiom.infrastructure.database.metadata import artifacts
from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.common.ids import allocate_readable_id


class ArtifactRepository:
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
