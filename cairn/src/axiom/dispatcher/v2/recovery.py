from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import Engine, select, update

from axiom.infrastructure.database.metadata import intents, reason_workspaces, tasks
from axiom.modules.common.time import utcnow


class RecoveryResult(BaseModel):
    intents_released: int
    reasons_released: int
    workspaces_abandoned: int = 0


class LeaseRecoveryService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def recover(self, *, timeout_seconds: int) -> RecoveryResult:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).isoformat().replace("+00:00", "Z")
        now = utcnow()
        with self.engine.begin() as conn:
            intent_result = conn.execute(
                update(intents)
                .where(
                    intents.c.status.in_(["claimed", "running", "concluding"]),
                    intents.c.last_heartbeat_at < cutoff,
                )
                .values(
                    status="pending",
                    worker=None,
                    last_heartbeat_at=None,
                    retry_count=intents.c.retry_count + 1,
                    next_retry_at=now,
                    last_failure_kind="stale_lease_recovered",
                )
            )
            reason_result = conn.execute(
                update(tasks)
                .where(
                    tasks.c.reason_worker.is_not(None),
                    tasks.c.reason_heartbeat_at < cutoff,
                )
                .values(
                    reason_worker=None,
                    reason_heartbeat_at=None,
                    phase="reason",
                    updated_at=now,
                )
            )
            workspace_result = conn.execute(
                update(reason_workspaces)
                .where(
                    reason_workspaces.c.status == "open",
                    reason_workspaces.c.task_id.in_(
                        select(tasks.c.id).where(tasks.c.reason_worker.is_(None))
                    ),
                    reason_workspaces.c.updated_at < cutoff,
                )
                .values(
                    status="abandoned",
                    failure_reason="stale Reason lease recovered after Dispatcher restart",
                    updated_at=now,
                )
            )
        return RecoveryResult(
            intents_released=intent_result.rowcount or 0,
            reasons_released=reason_result.rowcount or 0,
            workspaces_abandoned=workspace_result.rowcount or 0,
        )
