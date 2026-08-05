from __future__ import annotations

import json

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from axiom.modules.workers.schemas import UpsertWorker, Worker
from axiom.modules.workers.tables import workers


class WorkerRepository:
    def upsert(
        self,
        conn: Connection,
        *,
        name: str,
        command: UpsertWorker,
        now: str,
    ) -> Worker:
        values = {
            "name": name,
            "worker_type": command.worker_type,
            "capabilities_json": json.dumps(command.capabilities),
            "max_running": command.max_running,
            "priority": command.priority,
            "config_json": json.dumps(command.config, ensure_ascii=False),
            "disabled": command.disabled,
            "created_at": now,
            "updated_at": now,
        }
        statement = pg_insert(workers).values(**values).on_conflict_do_update(
            index_elements=["name"],
            set_={
                key: value
                for key, value in values.items()
                if key not in {"name", "created_at"}
            },
        )
        conn.execute(statement)
        result = self.get(conn, name)
        assert result is not None
        return result

    def get(self, conn: Connection, name: str) -> Worker | None:
        row = conn.execute(select(workers).where(workers.c.name == name)).mappings().one_or_none()
        return self._worker(row) if row is not None else None

    def list(self, conn: Connection) -> list[Worker]:
        rows = conn.execute(
            select(workers).order_by(workers.c.priority, workers.c.name.collate("C"))
        ).mappings()
        return [self._worker(row) for row in rows]

    def set_cooldown(
        self,
        conn: Connection,
        *,
        name: str,
        cooldown_until: str | None,
        now: str,
    ) -> Worker | None:
        result = conn.execute(
            workers.update()
            .where(workers.c.name == name)
            .values(cooldown_until=cooldown_until, updated_at=now)
        )
        return self.get(conn, name) if result.rowcount else None

    def delete(self, conn: Connection, name: str) -> bool:
        return bool(conn.execute(delete(workers).where(workers.c.name == name)).rowcount)

    @staticmethod
    def _worker(row) -> Worker:
        return Worker(
            name=row["name"],
            worker_type=row["worker_type"],
            capabilities=json.loads(row["capabilities_json"] or "[]"),
            max_running=row["max_running"],
            priority=row["priority"],
            config=json.loads(row["config_json"] or "{}"),
            disabled=bool(row["disabled"]),
            cooldown_until=row["cooldown_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
