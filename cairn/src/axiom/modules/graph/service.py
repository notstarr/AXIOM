from __future__ import annotations

import json

from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import artifacts, facts, intent_sources, intents, tasks
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.common.time import utcnow
from axiom.modules.graph.schemas import ConcludeResult, Fact, Intent
from axiom.modules.tasks.repository import TaskRepository


ACTIVE_INTENT_STATUSES = {"claimed", "running", "concluding"}
REQUIRED_ARTIFACTS = {
    "bootstrap": {"asset_inventory", "initial_leads", "findings"},
    "explore": {"findings", "leads"},
}


class IntentService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)

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
            task = self._require_running_task(conn, task_id)
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
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker,
                    event_type="intent.claimed",
                    message=f"Intent {intent_id} claimed by {worker}",
                ),
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
            task = self.tasks.get(conn, task_id)
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
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id if task else None,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker,
                    event_type="intent.released",
                    level="warning" if failure_kind else "info",
                    message=f"Intent {intent_id} released",
                    payload={"failure_kind": failure_kind, "next_retry_at": next_retry_at},
                ),
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
            task = self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            phase = "bootstrap" if intent_id == "i001" else "explore"
            artifact_types = set(
                conn.execute(
                    select(artifacts.c.artifact_type).where(
                        artifacts.c.task_id == task_id,
                        artifacts.c.intent_id == intent_id,
                        artifacts.c.archived.is_(False),
                    )
                ).scalars()
            )
            missing = sorted(REQUIRED_ARTIFACTS[phase] - artifact_types)
            if missing:
                raise ValueError(
                    f"Intent cannot conclude before required artifacts are registered: {', '.join(missing)}"
                )
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
                .values(
                    phase=self._phase_after_intent(conn, task_id, intent_id),
                    updated_at=now,
                )
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker,
                    event_type="intent.completed",
                    message=f"Intent {intent_id} concluded into {fact_id}",
                    payload={"fact_id": fact_id},
                ),
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

    def needs_replan(self, task_id: str, intent_id: str, *, reason: str) -> Intent:
        reason = reason.strip() or "retry budget exhausted"
        with self.engine.begin() as conn:
            task = self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            if intent.status != "pending":
                raise ValueError(f"Intent cannot be marked for replan: {intent.status}")
            now = utcnow()
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(
                    status="failed",
                    worker=None,
                    last_heartbeat_at=None,
                    next_retry_at=None,
                    last_failure_kind="retry_exhausted",
                    concluded_at=now,
                )
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    phase=self._phase_after_intent(conn, task_id, intent_id),
                    updated_at=now,
                )
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    intent_id=intent_id,
                    event_type="intent.retry_exhausted",
                    level="warning",
                    message=f"Intent {intent_id} requires replanning",
                    payload={"reason": reason},
                ),
            )
            return self._require_intent(conn, task_id, intent_id)

    @staticmethod
    def _phase_after_intent(conn, task_id: str, current_intent_id: str) -> str:
        remaining = conn.execute(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.id != current_intent_id,
                intents.c.status.in_(["pending", "claimed", "running", "concluding"]),
            ).limit(1)
        ).scalar_one_or_none()
        return "explore" if remaining is not None else "reason"

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
