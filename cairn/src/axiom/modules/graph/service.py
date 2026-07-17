from __future__ import annotations

import json

from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import facts, intent_sources, intents, tasks
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.common.time import utcnow
from axiom.modules.graph.schemas import ConcludeResult, Fact, Intent
from axiom.modules.tasks.repository import TaskRepository


ACTIVE_INTENT_STATUSES = {"claimed", "running", "concluding"}


class IntentService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.tasks = TaskRepository()

    def get(self, task_id: str, intent_id: str) -> Intent:
        with self.engine.connect() as conn:
            return self._require_intent(conn, task_id, intent_id)

    def list(self, task_id: str) -> list[Intent]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(intents)
                .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
                .order_by(intents.c.created_at, intents.c.id)
            ).mappings()
            return [self._to_model(conn, row) for row in rows]

    def claim(self, task_id: str, intent_id: str, *, worker: str) -> Intent:
        worker = self._worker(worker)
        now = utcnow()
        with self.engine.begin() as conn:
            self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            if intent.status in ACTIVE_INTENT_STATUSES:
                if intent.worker != worker:
                    raise ValueError(f"Intent is currently claimed by {intent.worker}")
                return intent
            if intent.status != "pending":
                raise ValueError(f"Intent is not claimable: {intent.status}")
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(status="claimed", worker=worker, last_heartbeat_at=now)
            )
            return self._require_intent(conn, task_id, intent_id)

    def heartbeat(self, task_id: str, intent_id: str, *, worker: str) -> Intent:
        worker = self._worker(worker)
        with self.engine.begin() as conn:
            self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(status="running", last_heartbeat_at=utcnow())
            )
            return self._require_intent(conn, task_id, intent_id)

    def release(
        self,
        task_id: str,
        intent_id: str,
        *,
        worker: str,
        failure_kind: str = "",
        next_retry_at: str | None = None,
    ) -> Intent:
        worker = self._worker(worker)
        with self.engine.begin() as conn:
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            values = {
                "status": "pending",
                "worker": None,
                "last_heartbeat_at": None,
                "next_retry_at": next_retry_at,
            }
            if failure_kind:
                values["retry_count"] = intent.retry_count + 1
                values["last_failure_kind"] = failure_kind
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(**values)
            )
            return self._require_intent(conn, task_id, intent_id)

    def conclude(
        self,
        task_id: str,
        intent_id: str,
        *,
        worker: str,
        description: str,
    ) -> ConcludeResult:
        worker = self._worker(worker)
        description = description.strip()
        if not description:
            raise ValueError("description must not be empty")
        now = utcnow()
        with self.engine.begin() as conn:
            self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            fact_id = allocate_readable_id(
                conn, scope=task_id, kind="fact", prefix="f"
            )
            conn.execute(
                facts.insert().values(
                    id=fact_id,
                    task_id=task_id,
                    description=description,
                    created_at=now,
                )
            )
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(
                    to_fact_id=fact_id,
                    status="completed",
                    last_heartbeat_at=now,
                    concluded_at=now,
                    next_retry_at=None,
                )
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(phase="reason", updated_at=now)
            )
            return ConcludeResult(
                fact=Fact(
                    id=fact_id,
                    task_id=task_id,
                    description=description,
                    created_at=now,
                ),
                intent=self._require_intent(conn, task_id, intent_id),
            )

    def _require_running_task(self, conn, task_id: str):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.legacy_readonly:
            raise ValueError("Legacy task is read-only")
        if task.status != "running":
            raise ValueError("Task is not running")
        return task

    def _require_intent(self, conn, task_id: str, intent_id: str) -> Intent:
        row = conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"Intent not found: {intent_id}")
        return self._to_model(conn, row)

    @staticmethod
    def _require_owner(intent: Intent, worker: str) -> None:
        if intent.status not in ACTIVE_INTENT_STATUSES:
            raise ValueError(f"Intent is not currently claimed: {intent.status}")
        if intent.worker != worker:
            raise ValueError(f"Intent is currently claimed by {intent.worker}")

    @staticmethod
    def _worker(worker: str) -> str:
        value = worker.strip()
        if not value:
            raise ValueError("worker must not be empty")
        return value

    @staticmethod
    def _to_model(conn, row) -> Intent:
        source_rows = conn.execute(
            select(intent_sources.c.fact_id)
            .where(
                intent_sources.c.task_id == row["task_id"],
                intent_sources.c.intent_id == row["id"],
            )
            .order_by(intent_sources.c.fact_id)
        )
        return Intent(
            id=row["id"],
            task_id=row["task_id"],
            todo_id=row["todo_id"],
            from_facts=[item.fact_id for item in source_rows],
            to_fact_id=row["to_fact_id"],
            description=row["description"],
            status=row["status"],
            creator=row["creator"],
            worker=row["worker"],
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            targets=json.loads(row["targets_json"] or "[]"),
            execute_timeout_seconds=row["execute_timeout_seconds"],
            conclude_timeout_seconds=row["conclude_timeout_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            retry_count=row["retry_count"],
            next_retry_at=row["next_retry_at"],
            last_failure_kind=row["last_failure_kind"],
            created_at=row["created_at"],
            concluded_at=row["concluded_at"],
            archived=bool(row["archived"]),
        )
