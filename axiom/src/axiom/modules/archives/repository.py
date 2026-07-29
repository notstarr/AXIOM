from __future__ import annotations

from sqlalchemy import Connection, select

from axiom.modules.archives.tables import archive_batch_members, archive_batches


class ArchiveRepository:
    @staticmethod
    def batch(conn: Connection, task_id: str, archive_id: str):
        return conn.execute(
            select(archive_batches).where(
                archive_batches.c.task_id == task_id,
                archive_batches.c.id == archive_id,
            )
        ).mappings().one_or_none()

    @staticmethod
    def list_batches(conn: Connection, task_id: str, status: str | None = None):
        query = select(archive_batches).where(archive_batches.c.task_id == task_id)
        if status is not None:
            query = query.where(archive_batches.c.status == status)
        return conn.execute(
            query.order_by(archive_batches.c.created_at.desc(), archive_batches.c.id.desc())
        ).mappings().all()

    @staticmethod
    def members(conn: Connection, task_id: str, archive_id: str):
        return conn.execute(
            select(archive_batch_members)
            .where(
                archive_batch_members.c.task_id == task_id,
                archive_batch_members.c.archive_id == archive_id,
            )
            .order_by(
                archive_batch_members.c.entity_type,
                archive_batch_members.c.entity_id,
            )
        ).mappings().all()
