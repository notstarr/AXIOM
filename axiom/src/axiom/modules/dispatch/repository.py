from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from sqlalchemy import Connection, func, select, update

from axiom.dispatcher.schemas import (
    DispatchLimits,
    DispatcherSnapshot,
    IntentSnapshot,
    ReasonRunSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)
from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs
from axiom.modules.settings.schemas import RuntimeConfig
from axiom.modules.tasks.tables import tasks
from axiom.modules.workers.repository import WorkerRepository
from axiom.modules.workers.tables import workers
from axiom.shared.clock import parse_utc


ACTIVE_STATUSES = ("claimed", "running", "concluding")
OPEN_STATUSES = ("pending", *ACTIVE_STATUSES)


class DispatchRepository:
    def __init__(self) -> None:
        self.workers = WorkerRepository()

    def snapshot(
        self,
        conn: Connection,
        *,
        config: RuntimeConfig,
        config_version: int,
    ) -> DispatcherSnapshot:
        task_rows = conn.execute(
            select(tasks.c.id, tasks.c.status, tasks.c.created_at)
            .order_by(tasks.c.created_at, tasks.c.id)
        ).mappings()
        tasks_by_id = {
            row["id"]: TaskSnapshot(
                id=row["id"],
                status=row["status"],
                created_at=parse_utc(row["created_at"]),
                intents=[],
                reason_runs=[],
            )
            for row in task_rows
        }

        intent_rows = conn.execute(
            select(
                intents.c.task_id,
                intents.c.id,
                intents.c.kind,
                intents.c.status,
                intents.c.worker_pool_json,
                intents.c.next_retry_at,
                intents.c.created_at,
                intents.c.claimed_at,
            )
            .where(
                intents.c.archived.is_(False),
                intents.c.status.in_(OPEN_STATUSES),
            )
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings()
        for row in intent_rows:
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
                    claimed_at=parse_utc(row["claimed_at"]) if row["claimed_at"] else None,
                )
            )

        reason_rows = conn.execute(
            select(
                reason_runs.c.task_id,
                reason_runs.c.id,
                reason_runs.c.status,
                reason_runs.c.worker_pool_json,
                reason_runs.c.next_retry_at,
                reason_runs.c.created_at,
                reason_runs.c.claimed_at,
            )
            .where(reason_runs.c.status.in_(OPEN_STATUSES))
            .order_by(reason_runs.c.created_at, reason_runs.c.id)
        ).mappings()
        for row in reason_rows:
            task = tasks_by_id.get(row["task_id"])
            if task is None:
                continue
            task.reason_runs.append(
                ReasonRunSnapshot(
                    task_id=row["task_id"],
                    id=row["id"],
                    status=row["status"],
                    worker_pool=json.loads(row["worker_pool_json"] or "[]"),
                    next_retry_at=(
                        parse_utc(row["next_retry_at"]) if row["next_retry_at"] else None
                    ),
                    created_at=parse_utc(row["created_at"]),
                    claimed_at=parse_utc(row["claimed_at"]) if row["claimed_at"] else None,
                )
            )

        last_claimed_by_task: dict[str, datetime] = {}
        for table in (intents, reason_runs):
            for row in conn.execute(
                select(table.c.task_id, func.max(table.c.claimed_at).label("claimed_at"))
                .where(table.c.claimed_at.is_not(None))
                .group_by(table.c.task_id)
            ).mappings():
                claimed_at = parse_utc(row["claimed_at"])
                previous = last_claimed_by_task.get(row["task_id"])
                if previous is None or claimed_at > previous:
                    last_claimed_by_task[row["task_id"]] = claimed_at
        for task in tasks_by_id.values():
            task.last_claimed_at = last_claimed_by_task.get(task.id)

        running = Counter()
        for row in conn.execute(
            select(intents.c.worker, func.count().label("count"))
            .where(
                intents.c.worker.is_not(None),
                intents.c.status.in_(ACTIVE_STATUSES),
            )
            .group_by(intents.c.worker)
        ).mappings():
            running[row["worker"]] += row["count"]
        for row in conn.execute(
            select(reason_runs.c.worker, func.count().label("count"))
            .where(
                reason_runs.c.worker.is_not(None),
                reason_runs.c.status.in_(ACTIVE_STATUSES),
            )
            .group_by(reason_runs.c.worker)
        ).mappings():
            running[row["worker"]] += row["count"]

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
        return DispatcherSnapshot(
            tasks=list(tasks_by_id.values()),
            workers=worker_snapshots,
            limits=DispatchLimits(
                config_version=config_version,
                global_max_active_runs=config.global_max_active_runs,
                global_max_active_tasks=config.global_max_active_tasks,
                per_task_max_active_runs=config.per_task_max_active_runs,
                per_task_explore_concurrency=config.per_task_explore_concurrency,
            ),
        )

    @staticmethod
    def task_run_state(conn: Connection, task_id: str):
        return conn.execute(
            select(tasks.c.status, tasks.c.run_deadline_at).where(tasks.c.id == task_id)
        ).mappings().one_or_none()

    @staticmethod
    def intent_row(conn: Connection, task_id: str, intent_id: str):
        return conn.execute(
            select(intents, tasks.c.project_id.label("project_id"))
            .join(tasks, tasks.c.id == intents.c.task_id)
            .where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()

    @staticmethod
    def reason_run_row(conn: Connection, task_id: str, reason_run_id: str):
        return conn.execute(
            select(reason_runs, tasks.c.project_id.label("project_id"))
            .join(tasks, tasks.c.id == reason_runs.c.task_id)
            .where(reason_runs.c.task_id == task_id, reason_runs.c.id == reason_run_id)
        ).mappings().one_or_none()

    @staticmethod
    def worker_row(conn: Connection, worker_name: str):
        return conn.execute(
            select(workers).where(workers.c.name == worker_name)
        ).mappings().one_or_none()

    @staticmethod
    def worker_active_count(conn: Connection, worker_name: str) -> int:
        intent_count = conn.execute(
            select(func.count())
            .select_from(intents)
            .where(intents.c.worker == worker_name, intents.c.status.in_(ACTIVE_STATUSES))
        ).scalar_one()
        reason_count = conn.execute(
            select(func.count())
            .select_from(reason_runs)
            .where(
                reason_runs.c.worker == worker_name,
                reason_runs.c.status.in_(ACTIVE_STATUSES),
            )
        ).scalar_one()
        return int(intent_count + reason_count)

    @staticmethod
    def global_active_count(conn: Connection) -> int:
        intent_count = conn.execute(
            select(func.count()).select_from(intents).where(intents.c.status.in_(ACTIVE_STATUSES))
        ).scalar_one()
        reason_count = conn.execute(
            select(func.count())
            .select_from(reason_runs)
            .where(reason_runs.c.status.in_(ACTIVE_STATUSES))
        ).scalar_one()
        return int(intent_count + reason_count)

    @staticmethod
    def active_task_ids(conn: Connection) -> set[str]:
        intent_task_ids = conn.execute(
            select(intents.c.task_id).where(intents.c.status.in_(ACTIVE_STATUSES)).distinct()
        ).scalars()
        reason_task_ids = conn.execute(
            select(reason_runs.c.task_id)
            .where(reason_runs.c.status.in_(ACTIVE_STATUSES))
            .distinct()
        ).scalars()
        return set(intent_task_ids) | set(reason_task_ids)

    @staticmethod
    def task_active_count(conn: Connection, task_id: str) -> int:
        intent_count = conn.execute(
            select(func.count())
            .select_from(intents)
            .where(intents.c.task_id == task_id, intents.c.status.in_(ACTIVE_STATUSES))
        ).scalar_one()
        reason_count = conn.execute(
            select(func.count())
            .select_from(reason_runs)
            .where(reason_runs.c.task_id == task_id, reason_runs.c.status.in_(ACTIVE_STATUSES))
        ).scalar_one()
        return int(intent_count + reason_count)

    @staticmethod
    def task_kind_active_count(conn: Connection, task_id: str, kind: str) -> int:
        if kind == "reason":
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(reason_runs)
                    .where(
                        reason_runs.c.task_id == task_id,
                        reason_runs.c.status.in_(ACTIVE_STATUSES),
                    )
                ).scalar_one()
            )
        return int(
            conn.execute(
                select(func.count())
                .select_from(intents)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind == kind,
                    intents.c.status.in_(ACTIVE_STATUSES),
                )
            ).scalar_one()
        )

    @staticmethod
    def claim_intent(
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        worker_name: str,
        now: str,
        lease_id: str,
        execute_timeout_seconds: int | None = None,
        conclude_timeout_seconds: int | None = None,
    ) -> bool:
        values = {
            "status": "claimed",
            "worker": worker_name,
            "lease_id": lease_id,
            "claimed_at": now,
            "last_heartbeat_at": now,
            "next_retry_at": None,
            "activity_kind": "claimed",
            "activity_summary": f"已由 {worker_name} 领取，正在启动执行环境",
            "activity_tool": "",
            "activity_started_at": now,
            "activity_updated_at": now,
        }
        if execute_timeout_seconds is not None:
            values["execute_timeout_seconds"] = execute_timeout_seconds
        if conclude_timeout_seconds is not None:
            values["conclude_timeout_seconds"] = conclude_timeout_seconds
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == intent_id,
                intents.c.status == "pending",
                intents.c.archived.is_(False),
            )
            .values(**values)
        )
        return bool(result.rowcount)

    @staticmethod
    def stale_active_intents(conn: Connection, *, cutoff: str):
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
                intents.c.status.in_(ACTIVE_STATUSES),
                intents.c.archived.is_(False),
                intents.c.lease_id.is_not(None),
                intents.c.last_heartbeat_at.is_not(None),
                intents.c.last_heartbeat_at <= cutoff,
            )
            .order_by(intents.c.last_heartbeat_at, intents.c.task_id, intents.c.id)
        ).mappings().all()

    @staticmethod
    def stale_active_reason_runs(conn: Connection, *, cutoff: str):
        return conn.execute(
            select(reason_runs, tasks.c.project_id.label("project_id"))
            .join(tasks, tasks.c.id == reason_runs.c.task_id)
            .where(
                tasks.c.status == "running",
                reason_runs.c.status.in_(ACTIVE_STATUSES),
                reason_runs.c.lease_id.is_not(None),
                reason_runs.c.last_heartbeat_at.is_not(None),
                reason_runs.c.last_heartbeat_at <= cutoff,
            )
            .order_by(
                reason_runs.c.last_heartbeat_at,
                reason_runs.c.task_id,
                reason_runs.c.id,
            )
        ).mappings().all()

    @staticmethod
    def recover_stale_intent(
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
                intents.c.status.in_(ACTIVE_STATUSES),
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
                activity_kind="released" if not terminal else "failed",
                activity_summary=(
                    "租约已过期，等待重新领取" if not terminal else "租约连续过期，Intent 已失败"
                ),
                activity_tool="",
                activity_updated_at=now,
                concluded_at=now if terminal else None,
            )
        )
        return bool(result.rowcount)
