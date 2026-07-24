from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import Connection, func, select, update

from axiom.dispatcher.schemas import (
    DispatcherSnapshot,
    IntentSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)
from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.modules.workers.repository import WorkerRepository
from axiom.modules.workers.tables import workers
from axiom.shared.clock import parse_utc


ACTIVE_INTENT_STATUSES = ("claimed", "running", "concluding")


class DispatchRepository:
    def __init__(self) -> None:
        self.workers = WorkerRepository()

    def snapshot(self, conn: Connection) -> DispatcherSnapshot:
        task_rows = conn.execute(
            select(tasks.c.id, tasks.c.status).order_by(tasks.c.created_at, tasks.c.id)
        ).mappings()
        tasks_by_id = {
            row["id"]: TaskSnapshot(id=row["id"], status=row["status"], intents=[])
            for row in task_rows
        }

        pending_rows = conn.execute(
            select(
                intents.c.task_id,
                intents.c.id,
                intents.c.kind,
                intents.c.status,
                intents.c.worker_pool_json,
                intents.c.next_retry_at,
                intents.c.created_at,
            )
            .where(intents.c.archived.is_(False), intents.c.status == "pending")
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings()
        for row in pending_rows:
            task = tasks_by_id.get(row["task_id"])
            if task is None:
                continue
            task.intents.append(
                IntentSnapshot(
                    task_id=row["task_id"],
                    id=row["id"],
                    kind=row["kind"],
                    status=row["status"],
                    worker_pool=json.loads(row["worker_pool_json"] or "[]"),
                    next_retry_at=(
                        parse_utc(row["next_retry_at"]) if row["next_retry_at"] else None
                    ),
                    created_at=parse_utc(row["created_at"]),
                )
            )

        running = Counter(
            {
                row["worker"]: row["count"]
                for row in conn.execute(
                    select(intents.c.worker, func.count().label("count"))
                    .where(
                        intents.c.worker.is_not(None),
                        intents.c.status.in_(ACTIVE_INTENT_STATUSES),
                    )
                    .group_by(intents.c.worker)
                ).mappings()
            }
        )
        worker_snapshots = [
            WorkerSnapshot(
                name=worker.name,
                capabilities=frozenset(worker.capabilities),
                max_running=worker.max_running,
                running=running[worker.name],
                priority=worker.priority,
                disabled=worker.disabled,
                cooldown_until=(
                    parse_utc(worker.cooldown_until) if worker.cooldown_until else None
                ),
            )
            for worker in self.workers.list(conn)
        ]
        return DispatcherSnapshot(tasks=list(tasks_by_id.values()), workers=worker_snapshots)

    @staticmethod
    def task_status(conn: Connection, task_id: str) -> str | None:
        return conn.execute(select(tasks.c.status).where(tasks.c.id == task_id)).scalar_one_or_none()

    @staticmethod
    def intent_row(conn: Connection, task_id: str, intent_id: str):
        return conn.execute(
            select(intents, tasks.c.project_id.label("project_id"))
            .join(tasks, tasks.c.id == intents.c.task_id)
            .where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()

    @staticmethod
    def worker_row(conn: Connection, worker_name: str):
        return conn.execute(
            select(workers).where(workers.c.name == worker_name)
        ).mappings().one_or_none()

    @staticmethod
    def active_count(conn: Connection, worker_name: str) -> int:
        return conn.execute(
            select(func.count())
            .select_from(intents)
            .where(
                intents.c.worker == worker_name,
                intents.c.status.in_(ACTIVE_INTENT_STATUSES),
            )
        ).scalar_one()

    @staticmethod
    def claim(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        worker_name: str,
        now: str,
        lease_id: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.status == "pending",
                intents.c.archived.is_(False),
            )
            .values(
                status="claimed",
                worker=worker_name,
                lease_id=lease_id,
                claimed_at=now,
                last_heartbeat_at=now,
                next_retry_at=None,
                activity_kind="claimed",
                activity_summary=f"已由 {worker_name} 领取，正在启动执行环境",
                activity_tool="",
                activity_started_at=now,
                activity_updated_at=now,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def stale_active(conn: Connection, *, cutoff: str):
        return conn.execute(
            select(
                intents.c.task_id,
                intents.c.id.label("intent_id"),
                intents.c.worker,
                intents.c.kind,
                intents.c.lease_id,
                intents.c.last_heartbeat_at,
                intents.c.infrastructure_retry_count,
                tasks.c.project_id,
            )
            .join(tasks, tasks.c.id == intents.c.task_id)
            .where(
                tasks.c.status == "running",
                intents.c.status.in_(ACTIVE_INTENT_STATUSES),
                intents.c.archived.is_(False),
                intents.c.lease_id.is_not(None),
                intents.c.last_heartbeat_at.is_not(None),
                intents.c.last_heartbeat_at <= cutoff,
            )
            .order_by(intents.c.last_heartbeat_at, intents.c.task_id, intents.c.id)
        ).mappings().all()

    @staticmethod
    def recover_stale(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        last_heartbeat_at: str,
        next_retry_at: str | None,
        terminal: bool,
        now: str,
    ) -> bool:
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.lease_id == lease_id,
                intents.c.last_heartbeat_at == last_heartbeat_at,
                intents.c.status.in_(ACTIVE_INTENT_STATUSES),
            )
            .values(
                status="failed" if terminal else "pending",
                worker=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                next_retry_at=None if terminal else next_retry_at,
                last_failure_kind="lease_expired",
                failure_class="transient",
                infrastructure_retry_count=intents.c.infrastructure_retry_count + 1,
                activity_kind="released",
                activity_summary="租约已过期，等待重新领取",
                activity_tool="",
                activity_updated_at=now,
                concluded_at=now if terminal else None,
            )
        )
        return bool(result.rowcount)
