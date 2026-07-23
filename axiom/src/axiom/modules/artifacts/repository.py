from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, select, update

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.artifacts.tables import artifacts
from axiom.shared.ids import allocate_id
from axiom.shared.storage import PreparedFile


BOOTSTRAP_ARTIFACT_TYPES = {
    "asset_inventory.md": "bootstrap.asset_inventory",
    "initial_leads.md": "bootstrap.initial_leads",
    "findings.md": "bootstrap.findings",
}


class ArtifactRepository:
    def create_upload(
        self,
        conn: Connection,
        *,
        task_id: str,
        prepared: PreparedFile,
        summary: str,
        created_by: str,
        now: str,
    ) -> Artifact:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="artifact", prefix="a"),
            "task_id": task_id,
            "intent_id": None,
            "artifact_type": "upload",
            "storage_key": prepared.storage_key,
            "summary": summary,
            "content_hash": prepared.content_hash,
            "size": prepared.size,
            "created_by": created_by,
            "created_at": now,
            "archived": False,
        }
        conn.execute(artifacts.insert().values(**values))
        return Artifact.model_validate(values)

    def create_bootstrap_bundle(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        prepared: Iterable[PreparedFile],
        created_by: str,
        now: str,
    ) -> list[Artifact]:
        records: list[Artifact] = []
        for item in prepared:
            filename = item.final_path.name
            values = {
                "id": allocate_id(
                    conn,
                    scope=task_id,
                    kind="artifact",
                    prefix="a",
                ),
                "task_id": task_id,
                "intent_id": intent_id,
                "artifact_type": BOOTSTRAP_ARTIFACT_TYPES[filename],
                "storage_key": item.storage_key,
                "summary": "",
                "content_hash": item.content_hash,
                "size": item.size,
                "created_by": created_by,
                "created_at": now,
                "archived": False,
            }
            conn.execute(artifacts.insert().values(**values))
            records.append(Artifact.model_validate(values))
        return records

    def create_explore_bundle(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        prepared: Iterable[PreparedFile],
        specs: dict[str, tuple[str, str]],
        created_by: str,
        now: str,
    ) -> list[Artifact]:
        records: list[Artifact] = []
        for item in prepared:
            filename = item.final_path.name
            artifact_type, summary = specs[filename]
            values = {
                "id": allocate_id(conn, scope=task_id, kind="artifact", prefix="a"),
                "task_id": task_id,
                "intent_id": intent_id,
                "artifact_type": artifact_type,
                "storage_key": item.storage_key,
                "summary": summary,
                "content_hash": item.content_hash,
                "size": item.size,
                "created_by": created_by,
                "created_at": now,
                "archived": False,
            }
            conn.execute(artifacts.insert().values(**values))
            records.append(Artifact.model_validate(values))
        return records

    def list(self, conn: Connection, task_id: str) -> list[Artifact]:
        rows = conn.execute(
            select(artifacts)
            .where(artifacts.c.task_id == task_id, artifacts.c.archived.is_(False))
            .order_by(artifacts.c.created_at, artifacts.c.id)
        ).mappings()
        return [Artifact.model_validate(row) for row in rows]

    def get(self, conn: Connection, task_id: str, artifact_id: str) -> Artifact | None:
        row = conn.execute(
            select(artifacts).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
                artifacts.c.archived.is_(False),
            )
        ).mappings().one_or_none()
        return Artifact.model_validate(row) if row is not None else None

    def archive(self, conn: Connection, task_id: str, artifact_id: str) -> Artifact | None:
        result = conn.execute(
            update(artifacts)
            .where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
                artifacts.c.archived.is_(False),
            )
            .values(archived=True)
        )
        if not result.rowcount:
            return None
        row = conn.execute(
            select(artifacts).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
            )
        ).mappings().one()
        return Artifact.model_validate(row)

    def for_intent(self, conn: Connection, task_id: str, intent_id: str) -> list[Artifact]:
        rows = conn.execute(
            select(artifacts)
            .where(
                artifacts.c.task_id == task_id,
                artifacts.c.intent_id == intent_id,
                artifacts.c.archived.is_(False),
            )
            .order_by(artifacts.c.id)
        ).mappings()
        return [Artifact.model_validate(row) for row in rows]
