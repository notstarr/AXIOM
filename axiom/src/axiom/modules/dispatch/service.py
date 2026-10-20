from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import Engine, select

from axiom.dispatcher.schemas import ClaimAction, ClaimedIntent, ClaimedWork, DispatcherSnapshot
from axiom.modules.dispatch.retry import retry_outcome
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.dispatch.repository import DispatchRepository
from axiom.modules.dispatch.schemas import (
    RecoveredIntent,
    RecoveredPlanRun,
    RecoveredReasonRun,
    RecoveredReviewRun,
    RecoveryReport,
)
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.schemas import ClaimedReasonRun
from axiom.modules.plan.repository import PlanRepository
from axiom.modules.plan.schemas import ClaimedPlanRun
from axiom.modules.graph.tables import intents
from axiom.modules.vulnerability_reviews.schemas import ClaimedVulnerabilityReviewRun
from axiom.modules.vulnerability_reviews.service import VulnerabilityReviewService
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.settings.tables import runtime_settings
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import run_transaction, transaction
from axiom.shared.errors import ConflictError, NotFoundError
from axiom.shared.ids import new_lease_id


class DispatchService:
    def __init__(self, engine: Engine, *, reviews: VulnerabilityReviewService):
        self.engine = engine
        self.repository = DispatchRepository()
        self.reason = ReasonRepository()
        self.plan = PlanRepository()
        self.reviews = reviews
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
        if action.reason_run_id:
            return self.claim_reason_run(
                task_id=action.task_id,
                reason_run_id=action.reason_run_id,
                worker_name=action.worker_name,
            )
        if action.plan_run_id:
            return self.claim_plan_run(
                task_id=action.task_id,
                plan_run_id=action.plan_run_id,
                worker_name=action.worker_name,
            )
        assert action.review_run_id is not None
        return self.claim_review_run(
            task_id=action.task_id,
            review_run_id=action.review_run_id,
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

        def claim(conn):
            if self.repository.lock_task(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if self.repository.lock_worker(conn, worker_name) is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            intent = self.repository.intent_row(conn, task_id, intent_id)
            if intent is None:
                raise NotFoundError(f"Intent not found: {task_id}/{intent_id}")
            if intent["kind"] == "reason":
                raise ConflictError("Legacy Reason Intent is read-only and cannot be scheduled")
            if (
                intent["kind"] in {"bootstrap", "explore"}
                and self.plan.in_flight_for_task(conn, task_id) is not None
            ):
                raise ConflictError("Task 已有正在执行的 PlanRun，增量 Intent 需等待前序结束")
            config = self._validate_claim(
                conn,
                task_id=task_id,
                worker_name=worker_name,
                kind=intent["kind"],
                status=intent["status"],
                archived=bool(intent["archived"]),
                next_retry_at=intent["next_retry_at"],
                infrastructure_retry_count=int(intent["infrastructure_retry_count"] or 0),
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
            return intent

        intent = run_transaction(self.engine, claim)
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

        def claim(conn):
            if self.repository.lock_task(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if self.repository.lock_worker(conn, worker_name) is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            run = self.repository.reason_run_row(conn, task_id, reason_run_id)
            if run is None:
                raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
            if run["trigger"] == "plan":
                raise ConflictError(
                    "Legacy ReasonRun(trigger=plan) is read-only; claim the independent PlanRun"
                )
            if self.plan.in_flight_for_task(conn, task_id) is not None:
                raise ConflictError("Task 已有正在执行的 PlanRun，Reason 需等待前序结束")
            in_flight = self.reason.in_flight_for_task(conn, task_id)
            if (
                in_flight is not None
                and in_flight.trigger != "plan"
                and in_flight.id != reason_run_id
            ):
                raise ConflictError(
                    f"Task 已有正在执行的 ReasonRun {in_flight.id}，"
                    f"排队的 {reason_run_id} 需等待前序结束"
                )
            pending = self.reason.pending_runs_for_task(conn, task_id)
            regular_pending = [item for item in pending if item.trigger != "plan"]
            expected = (
                regular_pending[0]
                if regular_pending
                else pending[0]
                if pending
                else None
            )
            if run["status"] == "pending" and (
                expected is None or expected.id != reason_run_id
            ):
                raise ConflictError(
                    f"ReasonRun {reason_run_id} 不是 Task 的 FIFO 队首，"
                    f"当前队首为 {expected.id if expected else 'none'}"
                )
            config = self._validate_claim(
                conn,
                task_id=task_id,
                worker_name=worker_name,
                kind="reason",
                status=run["status"],
                archived=False,
                next_retry_at=run["next_retry_at"],
                infrastructure_retry_count=int(run["infrastructure_retry_count"] or 0),
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
                        "phase": "plan" if run["trigger"] == "plan" else "reason",
                        "lease_id": lease_id,
                        "trigger": run["trigger"],
                    },
                ),
            )
            return run

        run_transaction(self.engine, claim)
        return ClaimedReasonRun(
            task_id=task_id,
            reason_run_id=reason_run_id,
            worker_name=worker_name,
            claimed_at=now,
            lease_id=lease_id,
        )

    def claim_plan_run(
        self,
        *,
        task_id: str,
        plan_run_id: str,
        worker_name: str,
    ) -> ClaimedPlanRun:
        """Claim the FIFO head of the independent Plan queue.

        Plan is intentionally validated and persisted through PlanRepository;
        no ``reason_runs.trigger`` row is eligible here.  The Task lock and
        the active Reason check keep the two planning phases mutually
        exclusive during reconciliation races.
        """

        now = utcnow()
        lease_id = new_lease_id()

        def claim(conn):
            if self.repository.lock_task(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if self.repository.lock_worker(conn, worker_name) is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            run = self.repository.plan_run_row(conn, task_id, plan_run_id)
            if run is None:
                raise NotFoundError(f"PlanRun not found: {task_id}/{plan_run_id}")
            if self.plan.in_flight_for_task(conn, task_id) is not None:
                raise ConflictError(
                    f"Task 已有正在执行的 PlanRun，排队的 {plan_run_id} 需等待前序结束"
                )
            reason_in_flight = self.reason.in_flight_for_task(conn, task_id)
            if reason_in_flight is not None:
                raise ConflictError(
                    f"Task 已有正在执行的 ReasonRun {reason_in_flight.id}，Plan 需等待前序结束"
                )
            incremental_owner = conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind.in_({"bootstrap", "explore"}),
                    intents.c.status.in_({"pending", "claimed", "running", "concluding"}),
                    intents.c.archived.is_(False),
                )
                .order_by(intents.c.created_at, intents.c.id)
                .limit(1)
            ).scalar_one_or_none()
            if incremental_owner is not None:
                raise ConflictError(
                    f"Task has pending or active Explore/Bootstrap Intent {incremental_owner}; Plan must wait"
                )
            pending_reason = next(
                (
                    item
                    for item in self.reason.pending_runs_for_task(conn, task_id)
                    if item.trigger != "plan"
                ),
                None,
            )
            if pending_reason is not None:
                raise ConflictError(
                    f"Task 已有排队中的 ReasonRun {pending_reason.id}，Plan 需等待前序结束"
                )
            pending = self.plan.pending_runs_for_task(conn, task_id)
            expected = pending[0] if pending else None
            if run["status"] == "pending" and (
                expected is None or expected.id != plan_run_id
            ):
                raise ConflictError(
                    f"PlanRun {plan_run_id} 不是 Task 的 FIFO 队首，"
                    f"当前队首为 {expected.id if expected else 'none'}"
                )
            config = self._validate_claim(
                conn,
                task_id=task_id,
                worker_name=worker_name,
                kind="plan",
                status=run["status"],
                archived=False,
                next_retry_at=run["next_retry_at"],
                infrastructure_retry_count=int(run["infrastructure_retry_count"] or 0),
                worker_pool=json.loads(run["worker_pool_json"] or "[]"),
                now=now,
                owner_label="PlanRun",
            )
            settings_version = conn.execute(
                select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
            ).scalar_one_or_none() or 1
            if not self.plan.claim(
                conn,
                task_id=task_id,
                run_id=plan_run_id,
                worker_name=worker_name,
                lease_id=lease_id,
                settings_version=settings_version,
                # Stage budgets are snapshotted when the lease is claimed.
                # Plan has its own settings; never fall back to Reason's
                # budget or preserve a stale pre-claim value here.
                execute_timeout_seconds=getattr(config, "plan_execute_seconds", 600),
                conclude_timeout_seconds=getattr(config, "plan_conclude_seconds", 300),
                now=now,
            ):
                raise ConflictError("PlanRun claim lost a concurrent race")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=run["project_id"],
                    task_id=task_id,
                    plan_run_id=plan_run_id,
                    worker=worker_name,
                    event_type="plan_run.claimed",
                    message=f"PlanRun 已由 Plan Worker {worker_name} 领取",
                    payload={
                        "phase": "plan",
                        "lease_id": lease_id,
                    },
                ),
            )
            return run

        run_transaction(self.engine, claim)
        return ClaimedPlanRun(
            task_id=task_id,
            plan_run_id=plan_run_id,
            worker_name=worker_name,
            claimed_at=now,
            lease_id=lease_id,
        )

    def claim_review_run(
        self,
        *,
        task_id: str,
        review_run_id: str,
        worker_name: str,
    ) -> ClaimedVulnerabilityReviewRun:
        now = utcnow()
        lease_id = new_lease_id()

        def claim(conn):
            if self.repository.lock_task(conn, task_id) is None:
                raise NotFoundError(f"Task not found: {task_id}")
            if self.repository.lock_worker(conn, worker_name) is None:
                raise NotFoundError(f"Worker not found: {worker_name}")
            run = self.repository.review_run_row(conn, task_id, review_run_id)
            if run is None:
                raise NotFoundError(
                    f"Vulnerability ReviewRun not found: {task_id}/{review_run_id}"
                )
            config = self._validate_claim(
                conn,
                task_id=task_id,
                worker_name=worker_name,
                kind="review",
                status=run["status"],
                archived=False,
                next_retry_at=run["next_retry_at"],
                infrastructure_retry_count=int(run["infrastructure_retry_count"] or 0),
                worker_pool=json.loads(run["worker_pool_json"] or "[]"),
                now=now,
                owner_label="Vulnerability ReviewRun",
            )
            settings_version = conn.execute(
                select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
            ).scalar_one_or_none() or 1
            if not self.reviews.repository.claim(
                conn,
                task_id=task_id,
                run_id=review_run_id,
                worker_name=worker_name,
                lease_id=lease_id,
                settings_version=settings_version,
                execute_timeout_seconds=config.review_execute_seconds,
                conclude_timeout_seconds=config.review_conclude_seconds,
                now=now,
            ):
                raise ConflictError("Vulnerability ReviewRun claim lost a concurrent race")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=run["project_id"],
                    task_id=task_id,
                    review_run_id=review_run_id,
                    worker=worker_name,
                    event_type="vulnerability_review.claimed",
                    message=f"漏洞审核已由 Reviewer {worker_name} 领取",
                    payload={
                        "phase": "review",
                        "lease_id": lease_id,
                        "vulnerability_id": run["vulnerability_id"],
                    },
                ),
            )
            return run

        run = run_transaction(self.engine, claim)
        return ClaimedVulnerabilityReviewRun(
            task_id=task_id,
            review_run_id=review_run_id,
            vulnerability_id=run["vulnerability_id"],
            review_subject_type=run.get("review_subject_type", "vulnerability"),
            red_team_result_id=run.get("red_team_result_id"),
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
        infrastructure_retry_count: int,
        worker_pool: list[str],
        now: str,
        owner_label: str,
    ):
        task_state = self.repository.task_run_state(conn, task_id)
        if task_state is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if task_state["legacy_readonly"]:
            raise ConflictError("Legacy Task is read-only")
        if task_state["archived"]:
            raise ConflictError("已归档任务不再接受 Worker 调度")
        if task_state["status"] != "running":
            raise ConflictError("Only a running Task can dispatch work")
        if (
            kind != "review"
            and task_state["run_deadline_at"]
            and parse_utc(task_state["run_deadline_at"]) <= parse_utc(now)
        ):
            raise ConflictError("Task run deadline has elapsed")
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

        conn.execute(
            select(runtime_settings.c.id)
            .where(runtime_settings.c.id == "runtime")
            .with_for_update()
        ).scalar_one_or_none()
        config = load_runtime_config(conn)
        if infrastructure_retry_count >= config.infrastructure_retry_limit:
            raise ConflictError(
                f"{owner_label} infrastructure retry budget exhausted; "
                "manual or system recovery is required before another claim"
            )
        if self.repository.global_active_count(conn) >= config.global_max_active_runs:
            raise ConflictError("Global active run limit reached")
        active_task_ids = self.repository.active_task_ids(conn)
        if task_id not in active_task_ids and len(active_task_ids) >= config.global_max_active_tasks:
            raise ConflictError("Global active Task limit reached")
        if self.repository.task_active_count(conn, task_id) >= config.per_task_max_active_runs:
            raise ConflictError("Per-Task active run limit reached")
        if kind == "explore":
            kind_limit = config.per_task_explore_concurrency
        elif kind == "review":
            kind_limit = config.per_task_review_concurrency
        else:
            kind_limit = 1
        if self.repository.task_kind_active_count(conn, task_id, kind) >= kind_limit:
            raise ConflictError(f"Per-Task {kind} concurrency limit reached")
        return config

    def recover_stale(self, *, stale_after_seconds: int) -> RecoveryReport:
        now_dt = parse_utc(utcnow())
        now = format_utc(now_dt)
        cutoff = format_utc(now_dt - timedelta(seconds=stale_after_seconds))
        recovered: list[RecoveredIntent] = []
        recovered_reason_runs: list[RecoveredReasonRun] = []
        recovered_plan_runs: list[RecoveredPlanRun] = []
        recovered_review_runs: list[RecoveredReviewRun] = []
        with transaction(self.engine) as conn:
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
                                "phase": "plan" if row["trigger"] == "plan" else "reason",
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
            for row in self.repository.stale_active_plan_runs(conn, cutoff=cutoff):
                attempt = row["infrastructure_retry_count"] + 1
                terminal, next_retry_at = self._retry_outcome(config, attempt, now_dt)
                if self.plan.release(
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
                    self.plan.abandon_open(
                        conn,
                        task_id=row["task_id"],
                        plan_run_id=row["id"],
                        lease_id=row["lease_id"],
                        failure_reason="lease_expired",
                        now=now,
                    )
                    self.audit.record_in(
                        conn,
                        RecordAuditEvent(
                            project_id=row["project_id"],
                            task_id=row["task_id"],
                            plan_run_id=row["id"],
                            worker=row["worker"] or "",
                            event_type="plan_run.lease_expired",
                            level="warning",
                            message="PlanRun 租约已过期，已释放并进入重试队列",
                            payload={
                                "phase": "plan",
                                "expired_lease_id": row["lease_id"],
                                "next_retry_at": next_retry_at,
                                "terminal": terminal,
                            },
                        ),
                    )
                    recovered_plan_runs.append(
                        RecoveredPlanRun(
                            task_id=row["task_id"],
                            plan_run_id=row["id"],
                            worker_name=row["worker"] or "",
                            expired_lease_id=row["lease_id"],
                        )
                    )
            for row in self.repository.stale_active_review_runs(conn, cutoff=cutoff):
                attempt = row["infrastructure_retry_count"] + 1
                terminal, next_retry_at = self._retry_outcome(config, attempt, now_dt)
                if self.reviews.repository.release(
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
                    self.audit.record_in(
                        conn,
                        RecordAuditEvent(
                            project_id=row["project_id"],
                            task_id=row["task_id"],
                            review_run_id=row["id"],
                            worker=row["worker"] or "",
                            event_type="vulnerability_review.lease_expired",
                            level="warning",
                            message="Reviewer 租约已过期，已释放并进入重试队列",
                            payload={
                                "phase": "review",
                                "expired_lease_id": row["lease_id"],
                                "next_retry_at": next_retry_at,
                                "terminal": terminal,
                            },
                        ),
                    )
                    recovered_review_runs.append(
                        RecoveredReviewRun(
                            task_id=row["task_id"],
                            review_run_id=row["id"],
                            worker_name=row["worker"] or "",
                            expired_lease_id=row["lease_id"],
                        )
                    )
        return RecoveryReport(
            recovered=recovered,
            recovered_reason_runs=recovered_reason_runs,
            recovered_plan_runs=recovered_plan_runs,
            recovered_review_runs=recovered_review_runs,
        )

    @staticmethod
    def _retry_outcome(config, attempt: int, now) -> tuple[bool, str | None]:
        # Stale recovery is always an infrastructure failure: the row stays
        # pending at its limit instead of becoming terminal, and claim
        # validation parks it until an explicit recovery action.
        outcome = retry_outcome(
            config, attempt=attempt, failure_class="transient", now=format_utc(now)
        )
        return outcome.terminal, outcome.next_retry_at
