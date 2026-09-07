from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.sandboxes.tables import task_sandboxes


class TaskSandboxRepository:
    def get_for_task(self, conn: Connection, task_id: str, *, for_update: bool = False):
        query = select(task_sandboxes).where(task_sandboxes.c.task_id == task_id)
        if for_update:
            query = query.with_for_update()
        return conn.execute(query).mappings().one_or_none()

    def insert(self, conn: Connection, values: dict) -> None:
        conn.execute(task_sandboxes.insert().values(**values))

    def update_status(self, conn: Connection, *, task_id: str, status: str, now: str, error: str = "") -> None:
        values = {
            "status": status,
            "last_used_at": now,
            "last_error": error,
        }
        if status == "terminated":
            values["terminated_at"] = now
        elif status == "ready":
            values["terminated_at"] = None
        conn.execute(
            update(task_sandboxes)
            .where(task_sandboxes.c.task_id == task_id)
            .values(**values)
        )

    def touch(self, conn: Connection, *, task_id: str, now: str) -> None:
        conn.execute(
            update(task_sandboxes)
            .where(task_sandboxes.c.task_id == task_id)
            .values(last_used_at=now)
        )

    def record_healthcheck(self, conn: Connection, *, task_id: str, now: str, error: str = "") -> None:
        conn.execute(
            update(task_sandboxes)
            .where(task_sandboxes.c.task_id == task_id)
            .values(
                status="ready" if not error else "degraded",
                last_used_at=now,
                last_healthcheck_at=now,
                last_healthcheck_error=error,
                last_error=error,
            )
        )

    @staticmethod
    def to_model(row) -> dict:
        if row is None:
            return None
        result = dict(row)
        for key in ("resource_profile_json", "network_policy_json"):
            value = result.get(key)
            if isinstance(value, str):
                try:
                    result[key.removesuffix("_json")] = json.loads(value or "{}")
                except json.JSONDecodeError:
                    result[key.removesuffix("_json")] = {}
            else:
                result[key.removesuffix("_json")] = value or {}
            result.pop(key, None)
        return result
