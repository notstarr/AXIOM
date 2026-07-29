from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, func, or_, select, update

from axiom.modules.artifacts.schemas import Artifact
from axiom.modules.artifacts.tables import artifacts
from axiom.modules.projects.tables import projects
from axiom.modules.tasks.tables import tasks
from axiom.shared.ids import allocate_id
from axiom.shared.storage import PreparedFile


BOOTSTRAP_ARTIFACT_TYPES = {
    "asset_inventory.md": "bootstrap.asset_inventory",
    "initial_leads.md": "bootstrap.initial_leads",
    "findings.md": "bootstrap.findings",
}

BOOTSTRAP_ARTIFACT_SUMMARIES = {
    "asset_inventory.md": "Bootstrap 资产清单",
    "initial_leads.md": "Bootstrap 初始线索",
    "findings.md": "Bootstrap 已确认问题",
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
            filename = item.relative_name
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
                "summary": BOOTSTRAP_ARTIFACT_SUMMARIES[filename],
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
            filename = item.relative_name
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

    def list_page(
        self,
        conn: Connection,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
        search: str = "",
        artifact_type: str | None = None,
        phase: str | None = None,
        source: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[Artifact], int]:
        query = select(artifacts).join(tasks, tasks.c.id == artifacts.c.task_id)
        query = query.where(artifacts.c.archived.is_(False))
        if task_id:
            query = query.where(artifacts.c.task_id == task_id)
        if project_id:
            query = query.where(tasks.c.project_id == project_id)
        if artifact_type:
            query = query.where(artifacts.c.artifact_type == artifact_type)
        if phase:
            query = query.where(artifacts.c.artifact_type.like(f"{phase}.%"))
        if source == "upload":
            query = query.where(artifacts.c.artifact_type == "upload")
        elif source == "worker":
            query = query.where(artifacts.c.artifact_type != "upload")
        if search.strip():
            pattern = f"%{search.strip()}%"
            query = query.join(projects, projects.c.id == tasks.c.project_id).where(
                or_(
                    artifacts.c.id.ilike(pattern),
                    artifacts.c.intent_id.ilike(pattern),
                    artifacts.c.storage_key.ilike(pattern),
                    artifacts.c.summary.ilike(pattern),
                    artifacts.c.artifact_type.ilike(pattern),
                    tasks.c.title.ilike(pattern),
                    projects.c.title.ilike(pattern),
                )
            )
        total = conn.execute(
            select(func.count()).select_from(query.order_by(None).subquery())
        ).scalar_one()
        rows = conn.execute(
            query.order_by(
                artifacts.c.created_at.desc(),
                artifacts.c.task_id.desc(),
                artifacts.c.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).mappings()
        return [Artifact.model_validate(row) for row in rows], int(total)

    def get(self, conn: Connection, task_id: str, artifact_id: str) -> Artifact | None:
        row = conn.execute(
            select(artifacts).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
                artifacts.c.archived.is_(False),
            )
        ).mappings().one_or_none()
        return Artifact.model_validate(row) if row is not None else None

    def get_any(self, conn: Connection, task_id: str, artifact_id: str) -> Artifact | None:
        row = conn.execute(
            select(artifacts).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
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
