from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import Engine

from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.dispatch.repository import DispatchRepository
from axiom.modules.dispatch.schemas import RecoveredIntent, RecoveryReport
from axiom.modules.settings.service import load_runtime_config
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.ids import new_lease_id


class DispatchService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = DispatchRepository()
        self.audit = AuditService(engine)

    def snapshot(self) -> DispatcherSnapshot:
        with self.engine.connect() as conn:
            return self.repository.snapshot(conn)

    def claim(self, *, task_id: str, intent_id: str, worker_name: str) -> ClaimedIntent:
        now = utcnow()
        lease_id = new_lease_id()
        with immediate_transaction(self.engine) as conn:
            task_status = self.repository.task_status(conn, task_id)
            if task_status is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if task_status != "running":
                raise ConflictError("Only a running Task can dispatch Intents")

            intent = self.repository.intent_row(conn, task_id, intent_id)
            if intent is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if intent["status"] != "pending" or intent["archived"]:
                raise ConflictError("Intent is no longer pending")
            if intent["next_retry_at"] and parse_utc(intent["next_retry_at"]) > parse_utc(now):
                raise ConflictError("Intent retry delay has not elapsed")

            worker = self.repository.worker_row(conn, worker_name)
            if worker is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            if worker["disabled"]:
                raise ConflictError("Worker is disabled")
            if worker["cooldown_until"] and parse_utc(worker["cooldown_until"]) > parse_utc(now):
                raise ConflictError("Worker is cooling down")
            capabilities = json.loads(worker["capabilities_json"] or "[]")
            if intent["kind"] not in capabilities:
                raise ConflictError("Worker does not support this Intent kind")
            worker_pool = json.loads(intent["worker_pool_json"] or "[]")
            if worker_pool and worker_name not in worker_pool:
                raise ConflictError("Worker is outside the Intent worker pool")
            if self.repository.active_count(conn, worker_name) >= worker["max_running"]:
                raise ConflictError("Worker has no free capacity")

            if not self.repository.claim(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                worker_name=worker_name,
                now=now,
                lease_id=lease_id,
            ):
                raise ConflictError("Intent claim lost a concurrent race")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=intent["project_id"],
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker_name,
                    event_type="intent.claimed",
                    message=f"{intent['kind']} Intent 已由 Worker {worker_name} 领取",
                    payload={"phase": intent["kind"], "lease_id": lease_id},
                ),
            )

        return ClaimedIntent(
            task_id=task_id,
            intent_id=intent_id,
            worker_name=worker_name,
            kind=intent["kind"],
            claimed_at=now,
            lease_id=lease_id,
        )

    def recover_stale(self, *, stale_after_seconds: int) -> RecoveryReport:
        now = parse_utc(utcnow())
        cutoff = format_utc(now - timedelta(seconds=stale_after_seconds))
        recovered: list[RecoveredIntent] = []
        with immediate_transaction(self.engine) as conn:
            config = load_runtime_config(conn)
            for row in self.repository.stale_active(conn, cutoff=cutoff):
                attempt = row["infrastructure_retry_count"] + 1
                terminal = attempt >= config.infrastructure_retry_limit
                delay = min(
                    config.retry_max_seconds,
                    config.retry_base_seconds * (2 ** min(max(0, attempt - 1), 30)),
                )
                next_retry_at = format_utc(now + timedelta(seconds=delay)) if not terminal else None
                if self.repository.recover_stale(
                    conn,
                    task_id=row["task_id"],
                    intent_id=row["intent_id"],
                    lease_id=row["lease_id"],
                    last_heartbeat_at=row["last_heartbeat_at"],
                    next_retry_at=next_retry_at,
                    terminal=terminal,
                    now=format_utc(now),
                ):
                    self.audit.record_in(
                        conn,
                        RecordAuditEvent(
                            project_id=row["project_id"],
                            task_id=row["task_id"],
                            intent_id=row["intent_id"],
                            worker=row["worker"] or "",
                            event_type="intent.lease_expired",
                            level="warning",
                            message="Intent 租约已过期，已释放并进入重试队列",
                            payload={
                                "phase": row["kind"],
                                "expired_lease_id": row["lease_id"],
                                "next_retry_at": next_retry_at,
                                "terminal": terminal,
                            },
                        ),
                    )
                    recovered.append(
                        RecoveredIntent(
                            task_id=row["task_id"],
                            intent_id=row["intent_id"],
                            worker_name=row["worker"] or "",
                            expired_lease_id=row["lease_id"],
                        )
                    )
        return RecoveryReport(recovered=recovered)
