from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import Engine, select

from axiom.dispatcher.schemas import ClaimAction, ClaimedIntent, ClaimedWork, DispatcherSnapshot
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.dispatch.repository import DispatchRepository
from axiom.modules.dispatch.schemas import (
    RecoveredIntent,
    RecoveredReasonRun,
    RecoveryReport,
)
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.schemas import ClaimedReasonRun
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.settings.tables import runtime_settings
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import immediate_transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.ids import new_lease_id


class DispatchService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = DispatchRepository()
        self.reason = ReasonRepository()
        self.audit = AuditService(engine)

    def snapshot(self) -> DispatcherSnapshot:
        with self.engine.connect() as conn:
            config = load_runtime_config(conn)
            version = conn.execute(
                select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
            ).scalar_one_or_none() or 1
            return self.repository.snapshot(conn, config=config, config_version=version)

    def claim_work(self, action: ClaimAction) -> ClaimedWork:
        if action.intent_id:
            return self.claim_intent(
                task_id=action.task_id,
                intent_id=action.intent_id,
                worker_name=action.worker_name,
            )
        assert action.reason_run_id is not None
        return self.claim_reason_run(
            task_id=action.task_id,
            reason_run_id=action.reason_run_id,
            worker_name=action.worker_name,
        )

    def claim_intent(
        self,
        *,
        task_id: str,
        intent_id: str,
        worker_name: str,
    ) -> ClaimedIntent:
        now = utcnow()
        lease_id = new_lease_id()
        with immediate_transaction(self.engine) as conn:
            intent = self.repository.intent_row(conn, task_id, intent_id)
            if intent is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if intent["kind"] == "reason":
                raise ConflictError("Legacy Reason Intent is read-only and cannot be scheduled")
            config = self._validate_claim(
                conn,
                task_id=task_id,
                worker_name=worker_name,
                kind=intent["kind"],
                status=intent["status"],
                archived=bool(intent["archived"]),
                next_retry_at=intent["next_retry_at"],
                worker_pool=json.loads(intent["worker_pool_json"] or "[]"),
                now=now,
                owner_label="Intent",
            )
            if not self.repository.claim_intent(
                conn,
                task_id=task_id,
                intent_id=intent_id,
                worker_name=worker_name,
                now=now,
                lease_id=lease_id,
                execute_timeout_seconds=(
                    intent["execute_timeout_seconds"]
                    or config.bootstrap_execute_seconds
                    if intent["kind"] == "bootstrap"
                    else None
                ),
                conclude_timeout_seconds=(
                    intent["conclude_timeout_seconds"]
                    or config.bootstrap_conclude_seconds
                    if intent["kind"] == "bootstrap"
                    else None
                ),
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

    def claim_reason_run(
        self,
        *,
        task_id: str,
        reason_run_id: str,
        worker_name: str,
    ) -> ClaimedReasonRun:
        now = utcnow()
        lease_id = new_lease_id()
        with immediate_transaction(self.engine) as conn:
            run = self.repository.reason_run_row(conn, task_id, reason_run_id)
            if run is None:
                raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
            config = self._validate_claim(
                conn,
                task_id=task_id,
                worker_name=worker_name,
                kind="reason",
                status=run["status"],
                archived=False,
                next_retry_at=run["next_retry_at"],
                worker_pool=json.loads(run["worker_pool_json"] or "[]"),
                now=now,
                owner_label="ReasonRun",
            )
            settings_version = conn.execute(
                select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
            ).scalar_one_or_none() or 1
            if not self.reason.claim(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                worker_name=worker_name,
                lease_id=lease_id,
                settings_version=settings_version,
                execute_timeout_seconds=config.reason_execute_seconds,
                conclude_timeout_seconds=config.reason_conclude_seconds,
                target_open_intents=config.reason_target_open_intents,
                max_new_intents=config.reason_max_new_intents_per_run,
                explore_execute_min_seconds=config.explore_execute_min_seconds,
                explore_execute_max_seconds=config.explore_execute_max_seconds,
                explore_conclude_min_seconds=config.explore_conclude_min_seconds,
                explore_conclude_max_seconds=config.explore_conclude_max_seconds,
                now=now,
            ):
                raise ConflictError("ReasonRun claim lost a concurrent race")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=run["project_id"],
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    worker=worker_name,
                    event_type="reason_run.claimed",
                    message=f"ReasonRun 已由 Worker {worker_name} 领取",
                    payload={
                        "phase": "reason",
                        "lease_id": lease_id,
                        "trigger": run["trigger"],
                    },
                ),
            )
        return ClaimedReasonRun(
            task_id=task_id,
            reason_run_id=reason_run_id,
            worker_name=worker_name,
            claimed_at=now,
            lease_id=lease_id,
        )

    def _validate_claim(
        self,
        conn,
        *,
        task_id: str,
        worker_name: str,
        kind: str,
        status: str,
        archived: bool,
        next_retry_at: str | None,
        worker_pool: list[str],
        now: str,
        owner_label: str,
    ):
        task_status = self.repository.task_status(conn, task_id)
        if task_status is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if task_status != "running":
            raise ConflictError("Only a running Task can dispatch work")
        if status != "pending" or archived:
            raise ConflictError(f"{owner_label} is no longer pending")
        if next_retry_at and parse_utc(next_retry_at) > parse_utc(now):
            raise ConflictError("Work retry delay has not elapsed")

        worker = self.repository.worker_row(conn, worker_name)
        if worker is None:
            raise NotFoundError(f"Worker not found: {worker_name}")
        if worker["disabled"]:
            raise ConflictError("Worker is disabled")
        if worker["cooldown_until"] and parse_utc(worker["cooldown_until"]) > parse_utc(now):
            raise ConflictError("Worker is cooling down")
        capabilities = json.loads(worker["capabilities_json"] or "[]")
        if kind not in capabilities:
            raise ConflictError(f"Worker does not support {kind}")
        if worker_pool and worker_name not in worker_pool:
            raise ConflictError("Worker is outside the work item Worker Pool")
        if self.repository.worker_active_count(conn, worker_name) >= worker["max_running"]:
            raise ConflictError("Worker has no free capacity")

        config = load_runtime_config(conn)
        if self.repository.global_active_count(conn) >= config.global_max_active_runs:
            raise ConflictError("Global active run limit reached")
        active_task_ids = self.repository.active_task_ids(conn)
        if task_id not in active_task_ids and len(active_task_ids) >= config.global_max_active_tasks:
            raise ConflictError("Global active Task limit reached")
        if self.repository.task_active_count(conn, task_id) >= config.per_task_max_active_runs:
            raise ConflictError("Per-Task active run limit reached")
        kind_limit = config.per_task_explore_concurrency if kind == "explore" else 1
        if self.repository.task_kind_active_count(conn, task_id, kind) >= kind_limit:
            raise ConflictError(f"Per-Task {kind} concurrency limit reached")
        return config

    def recover_stale(self, *, stale_after_seconds: int) -> RecoveryReport:
        now_dt = parse_utc(utcnow())
        now = format_utc(now_dt)
        cutoff = format_utc(now_dt - timedelta(seconds=stale_after_seconds))
        recovered: list[RecoveredIntent] = []
        recovered_reason_runs: list[RecoveredReasonRun] = []
        with immediate_transaction(self.engine) as conn:
            config = load_runtime_config(conn)
            for row in self.repository.stale_active_intents(conn, cutoff=cutoff):
                attempt = row["infrastructure_retry_count"] + 1
                terminal, next_retry_at = self._retry_outcome(config, attempt, now_dt)
                if self.repository.recover_stale_intent(
                    conn,
                    task_id=row["task_id"],
                    intent_id=row["intent_id"],
                    lease_id=row["lease_id"],
                    last_heartbeat_at=row["last_heartbeat_at"],
                    next_retry_at=next_retry_at,
                    terminal=terminal,
                    now=now,
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

            for row in self.repository.stale_active_reason_runs(conn, cutoff=cutoff):
                attempt = row["infrastructure_retry_count"] + 1
                terminal, next_retry_at = self._retry_outcome(config, attempt, now_dt)
                if self.reason.release(
                    conn,
                    task_id=row["task_id"],
                    run_id=row["id"],
                    lease_id=row["lease_id"],
                    failure_kind="lease_expired",
                    failure_class="transient",
                    next_retry_at=next_retry_at,
                    terminal=terminal,
                    now=now,
                ):
                    self.reason.abandon_open(
                        conn,
                        task_id=row["task_id"],
                        reason_run_id=row["id"],
                        lease_id=row["lease_id"],
                        failure_reason="lease_expired",
                        now=now,
                    )
                    self.audit.record_in(
                        conn,
                        RecordAuditEvent(
                            project_id=row["project_id"],
                            task_id=row["task_id"],
                            reason_run_id=row["id"],
                            worker=row["worker"] or "",
                            event_type="reason_run.lease_expired",
                            level="warning",
                            message="ReasonRun 租约已过期，已释放并进入重试队列",
                            payload={
                                "phase": "reason",
                                "expired_lease_id": row["lease_id"],
                                "next_retry_at": next_retry_at,
                                "terminal": terminal,
                            },
                        ),
                    )
                    recovered_reason_runs.append(
                        RecoveredReasonRun(
                            task_id=row["task_id"],
                            reason_run_id=row["id"],
                            worker_name=row["worker"] or "",
                            expired_lease_id=row["lease_id"],
                        )
                    )
        return RecoveryReport(
            recovered=recovered,
            recovered_reason_runs=recovered_reason_runs,
        )

    @staticmethod
    def _retry_outcome(config, attempt: int, now) -> tuple[bool, str | None]:
        terminal = attempt >= config.infrastructure_retry_limit
        delay = min(
            config.retry_max_seconds,
            config.retry_base_seconds * (2 ** min(max(0, attempt - 1), 30)),
        )
        return terminal, None if terminal else format_utc(now + timedelta(seconds=delay))
