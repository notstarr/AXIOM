from __future__ import annotations

import json

from sqlalchemy import Engine, delete, select

from axiom.infrastructure.database.metadata import dispatch_workers, worker_runtime_states
from axiom.modules.common.time import utcnow
from axiom.modules.workers.schemas import UpsertWorker, Worker


class WorkerService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def upsert(self, body: UpsertWorker) -> Worker:
        now = utcnow()
        values = {
            "name": body.name,
            "worker_type": body.worker_type,
            "capabilities_json": json.dumps(body.capabilities),
            "max_running": body.max_running,
            "priority": body.priority,
            "config_json": json.dumps(body.config, ensure_ascii=False),
            "disabled": body.disabled,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(dispatch_workers.c.name).where(dispatch_workers.c.name == body.name)
            ).scalar_one_or_none()
            if existing is None:
                conn.execute(dispatch_workers.insert().values(**values))
            else:
                conn.execute(
                    dispatch_workers.update()
                    .where(dispatch_workers.c.name == body.name)
                    .values(**{key: value for key, value in values.items() if key != "name"})
                )
            return self._get(conn, body.name)

    def set_cooldown(
        self,
        worker_name: str,
        *,
        failure_kind: str,
        cooldown_until: str | None,
    ) -> Worker:
        now = utcnow()
        with self.engine.begin() as conn:
            self._get(conn, worker_name)
            existing = conn.execute(
                select(worker_runtime_states.c.worker_name).where(
                    worker_runtime_states.c.worker_name == worker_name
                )
            ).scalar_one_or_none()
            values = {
                "worker_name": worker_name,
                "cooldown_until": cooldown_until,
                "last_failure_kind": failure_kind,
                "updated_at": now,
            }
            if existing is None:
                conn.execute(worker_runtime_states.insert().values(**values))
            else:
                conn.execute(
                    worker_runtime_states.update()
                    .where(worker_runtime_states.c.worker_name == worker_name)
                    .values(**{key: value for key, value in values.items() if key != "worker_name"})
                )
            return self._get(conn, worker_name)

    def list(self) -> list[Worker]:
        with self.engine.connect() as conn:
            names = conn.execute(
                select(dispatch_workers.c.name).order_by(dispatch_workers.c.priority, dispatch_workers.c.name)
            ).scalars()
            return [self._get(conn, name) for name in names]

    def get(self, name: str) -> Worker:
        with self.engine.connect() as conn:
            return self._get(conn, name)

    def delete(self, name: str) -> None:
        with self.engine.begin() as conn:
            result = conn.execute(delete(dispatch_workers).where(dispatch_workers.c.name == name))
            if not result.rowcount:
                raise ValueError(f"Worker not found: {name}")

    @staticmethod
    def _get(conn, name: str) -> Worker:
        row = conn.execute(
            select(
                dispatch_workers,
                worker_runtime_states.c.cooldown_until,
                worker_runtime_states.c.last_failure_kind,
            )
            .outerjoin(
                worker_runtime_states,
                worker_runtime_states.c.worker_name == dispatch_workers.c.name,
            )
            .where(dispatch_workers.c.name == name)
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"Worker not found: {name}")
        return Worker(
            name=row["name"],
            worker_type=row["worker_type"],
            capabilities=json.loads(row["capabilities_json"]),
            max_running=row["max_running"],
            priority=row["priority"],
            config=json.loads(row["config_json"] or "{}"),
            disabled=bool(row["disabled"]),
            updated_at=row["updated_at"],
            cooldown_until=row["cooldown_until"],
            last_failure_kind=row["last_failure_kind"] or "",
        )
