from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import Engine, select

from axiom.dispatcher.v2.schemas import (
    DispatcherSnapshot,
    IntentSnapshot,
    TaskSnapshot,
    WorkerSnapshot,
)
from axiom.infrastructure.database.metadata import (
    dispatch_workers,
    intents,
    tasks,
    worker_runtime_states,
)


class DispatcherSnapshotService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def build(self) -> DispatcherSnapshot:
        with self.engine.connect() as conn:
            intent_rows = conn.execute(select(intents)).mappings().all()
            intents_by_task: dict[str, list[IntentSnapshot]] = {}
            running_by_worker: Counter[str] = Counter()
            for row in intent_rows:
                snapshot = IntentSnapshot(
                    id=row["id"],
                    status=row["status"],
                    creator=row["creator"],
                    retry_count=row["retry_count"],
                    next_retry_at=row["next_retry_at"],
                    last_failure_kind=row["last_failure_kind"],
                )
                intents_by_task.setdefault(row["task_id"], []).append(snapshot)
                if row["worker"] and row["status"] in {"claimed", "running", "concluding"}:
                    running_by_worker[row["worker"]] += 1

            task_rows = conn.execute(select(tasks).order_by(tasks.c.created_at, tasks.c.id)).mappings()
            task_snapshots: list[TaskSnapshot] = []
            for row in task_rows:
                if row["reason_worker"]:
                    running_by_worker[row["reason_worker"]] += 1
                task_snapshots.append(
                    TaskSnapshot(
                        id=row["id"],
                        status=row["status"],
                        phase=row["phase"],
                        reason_worker=row["reason_worker"],
                        reason_due=row["phase"] in {"reason", "waiting"},
                        worker_pool=json.loads(row["worker_pool_json"] or "[]"),
                        intents=intents_by_task.get(row["id"], []),
                    )
                )

            worker_rows = conn.execute(
                select(
                    dispatch_workers,
                    worker_runtime_states.c.cooldown_until,
                ).outerjoin(
                    worker_runtime_states,
                    worker_runtime_states.c.worker_name == dispatch_workers.c.name,
                )
            ).mappings()
            worker_snapshots = [
                WorkerSnapshot(
                    name=row["name"],
                    capabilities=json.loads(row["capabilities_json"]),
                    max_running=row["max_running"],
                    running=running_by_worker[row["name"]],
                    priority=row["priority"],
                    disabled=bool(row["disabled"]),
                    cooldown_until=row["cooldown_until"],
                )
                for row in worker_rows
            ]
            return DispatcherSnapshot(tasks=task_snapshots, workers=worker_snapshots)
