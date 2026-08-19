from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import timedelta

from sqlalchemy import Engine, exists, func, or_, select, update

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.context.tables import context_revisions
from axiom.modules.goals.repository import GoalRepository
from axiom.modules.goals.schemas import GoalAssessment
from axiom.modules.goals.service import GoalService
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.reason.repository import ReasonRepository
from axiom.modules.reason.schemas import (
    CommitResult,
    DraftIntent,
    DraftIntentAssessment,
    DraftTodo,
    DraftTodoDelete,
    DraftTodoUpdate,
    ReasonReconcileReport,
    ReasonRun,
    ReasonWorkspace,
    ReleasedReasonRun,
    StartedReasonRun,
    WorkspaceDraft,
    WorkspaceStatus,
)
from axiom.modules.reason.tables import reason_run_sources, reason_runs
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.settings.tables import runtime_settings
from axiom.modules.tasks.origin import parse_intent_network_target, scope_entry_matches_host
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.schemas import TaskHint
from axiom.modules.tasks.tables import tasks
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import (
    CreateTodo,
    TODO_STATUS_REASON_REQUIRED,
    TodoSource,
)
from axiom.modules.todos.tables import todos
from axiom.modules.workers.tables import workers
from axiom.shared.clock import format_utc, parse_utc, utcnow
from axiom.shared.database import run_transaction, transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError
from axiom.shared.ids import allocate_id, id_counters
from axiom.shared.storage import FileStore


logger = logging.getLogger(__name__)
OPEN_EXPLORE_STATUSES = ("pending", "claimed", "running", "concluding")
TERMINAL_EXPLORE_STATUSES = ("completed", "failed", "cancelled")
INTENT_ASSESSMENT_STATUSES = ("open", "partial", "blocked", "completed")
MAX_WORKSPACE_INTENTS = 256


class ReasonService:
    def __init__(self, engine: Engine, *, store: FileStore | None = None):
        self.engine = engine
        self.store = store
        self.repository = ReasonRepository()
        self.tasks = TaskRepository()
        self.todos = TodoRepository()
        self.goals = GoalRepository()
        self.audit = AuditService(engine)

    # ------------------------------------------------------------------
    # Trigger reconciliation
    # ------------------------------------------------------------------
    def create_initial(
        self,
        conn,
        *,
        task_id: str,
        source_fact_id: str,
        now: str,
    ) -> str | None:
        existing = self.repository.active_for_task(conn, task_id)
        if existing is not None:
            return existing.id
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if not task.is_runnable_at(now):
            return None
        fingerprint = self._fingerprint(
            source_fact_ids=[source_fact_id],
            failed_intent_ids=[],
            actionable_todo_ids=[],
            todo_review_ids=[],
            open_explore_ids=[],
            unassessed_intent_ids=[],
        )
        run = self._create_run(
            conn,
            task=task,
            source_fact_ids=[source_fact_id],
            trigger="bootstrap",
            trigger_payload={"source_fact_id": source_fact_id},
            input_fingerprint=fingerprint,
            now=now,
        )
        self._set_phase(conn, task_id, "reason", now)
        return run.id

    def advance_after_explore(self, conn, *, task_id: str, now: str) -> str | None:
        return self._reconcile_task_in(conn, task_id=task_id, now=now)

    def trigger_manual(self, task_id: str, *, actor: str) -> ReasonRun:
        now = utcnow()
        actor = actor.strip() or "人工"

        def trigger(conn):
            task_lock = conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            ).scalar_one_or_none()
            if task_lock is None:
                raise NotFoundError(f"Task not found: {task_id}")
            task = self.tasks.get(conn, task_id)
            assert task is not None
            if task.legacy_readonly:
                raise ConflictError("历史只读任务不能触发 Reason")
            if not task.is_runnable_at(now):
                raise ConflictError("只有运行中且未到期的任务可以触发 Reason")
            active = self.repository.active_for_task(conn, task_id)
            if active is not None:
                raise ConflictError(f"ReasonRun {active.id} 已在进行中")
            bootstrap_completed = conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind == "bootstrap",
                    intents.c.status == "completed",
                    intents.c.archived.is_(False),
                )
                .limit(1)
            ).scalar_one_or_none()
            if bootstrap_completed is None:
                raise ConflictError("Bootstrap 完成后才能触发 Reason")

            state = self._input_state(conn, task_id)
            source_fact_ids = [row["id"] for row in state["unplanned_facts"]]
            open_explore_ids = list(state["open_explore_ids"])
            failed_intent_ids = list(state["failed_intent_ids"])
            actionable_todo_ids = list(state["actionable_todo_ids"])
            todo_review_ids = list(state["todo_review_ids"])
            unassessed_intent_ids = list(state["unassessed_intent_ids"])
            fingerprint = self._fingerprint(
                source_fact_ids=source_fact_ids,
                failed_intent_ids=failed_intent_ids,
                actionable_todo_ids=actionable_todo_ids,
                todo_review_ids=todo_review_ids,
                open_explore_ids=open_explore_ids,
                unassessed_intent_ids=unassessed_intent_ids,
            )
            run = self._create_run(
                conn,
                task=task,
                source_fact_ids=source_fact_ids,
                trigger="manual",
                trigger_payload={
                    "actor": actor,
                    "requested_at": now,
                    "task_phase": task.phase,
                    "unplanned_fact_count": len(source_fact_ids),
                    "source_fact_ids": source_fact_ids,
                    "open_explore_count": len(open_explore_ids),
                    "open_explore_ids": open_explore_ids,
                    "failed_intent_ids": failed_intent_ids,
                    "actionable_todo_ids": actionable_todo_ids,
                    "todo_review_ids": todo_review_ids,
                    "unassessed_intent_ids": unassessed_intent_ids,
                },
                input_fingerprint=fingerprint,
                now=now,
            )
            self._set_phase(conn, task_id, "reason", now)
            return run

        return run_transaction(self.engine, trigger)

    def trigger_hint_in(
        self,
        conn,
        *,
        task_id: str,
        hint: TaskHint,
        now: str,
    ) -> ReasonRun | None:
        """Create an immediate ReasonRun for a newly added human Hint when possible.

        Hint persistence must not fail merely because the task cannot start a new
        ReasonRun.  The caller owns the transaction, so a successful trigger is
        committed atomically with the Hint that caused it.
        """

        task_lock = conn.execute(
            select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
        ).scalar_one_or_none()
        if task_lock is None:
            raise NotFoundError(f"Task not found: {task_id}")
        task = self.tasks.get(conn, task_id)
        if (
            task is None
            or task.legacy_readonly
            or not task.is_runnable_at(now)
            or self.repository.active_for_task(conn, task_id) is not None
        ):
            return None
        bootstrap_completed = conn.execute(
            select(intents.c.id)
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "bootstrap",
                intents.c.status == "completed",
                intents.c.archived.is_(False),
            )
            .limit(1)
        ).scalar_one_or_none()
        if bootstrap_completed is None:
            return None

        state = self._input_state(conn, task_id)
        source_fact_ids = [row["id"] for row in state["unplanned_facts"]]
        open_explore_ids = list(state["open_explore_ids"])
        failed_intent_ids = list(state["failed_intent_ids"])
        actionable_todo_ids = list(state["actionable_todo_ids"])
        todo_review_ids = list(state["todo_review_ids"])
        unassessed_intent_ids = list(state["unassessed_intent_ids"])
        base_fingerprint = self._fingerprint(
            source_fact_ids=source_fact_ids,
            failed_intent_ids=failed_intent_ids,
            actionable_todo_ids=actionable_todo_ids,
            todo_review_ids=todo_review_ids,
            open_explore_ids=open_explore_ids,
            unassessed_intent_ids=unassessed_intent_ids,
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "base": base_fingerprint,
                    "trigger": "hint",
                    "hint_id": hint.id,
                    "hint_content": hint.content,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        run = self._create_run(
            conn,
            task=task,
            source_fact_ids=source_fact_ids,
            trigger="hint",
            trigger_payload={
                "hint_id": hint.id,
                "hint_content": hint.content,
                "creator": hint.creator,
                "requested_at": now,
                "task_phase": task.phase,
                "unplanned_fact_count": len(source_fact_ids),
                "source_fact_ids": source_fact_ids,
                "open_explore_count": len(open_explore_ids),
                "open_explore_ids": open_explore_ids,
                "failed_intent_ids": failed_intent_ids,
                "actionable_todo_ids": actionable_todo_ids,
                "todo_review_ids": todo_review_ids,
                "unassessed_intent_ids": unassessed_intent_ids,
            },
            input_fingerprint=fingerprint,
            now=now,
        )
        self._set_phase(conn, task_id, "reason", now)
        return run

    def create_continuation_in(
        self,
        conn,
        *,
        task,
        previous_goal: str,
        continuation_reason: str,
        focus: str,
        actor: str,
        now: str,
    ) -> ReasonRun:
        state = self._input_state(conn, task.id)
        source_fact_ids = [row["id"] for row in state["unplanned_facts"]]
        open_explore_ids = list(state["open_explore_ids"])
        failed_intent_ids = list(state["failed_intent_ids"])
        actionable_todo_ids = list(state["actionable_todo_ids"])
        todo_review_ids = list(state["todo_review_ids"])
        unassessed_intent_ids = list(state["unassessed_intent_ids"])
        cancelled_intent_ids = list(
            conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task.id,
                    intents.c.status == "cancelled",
                    intents.c.last_failure_kind == "goal_achieved",
                    intents.c.archived.is_(False),
                )
                .order_by(intents.c.created_at, intents.c.id)
            ).scalars()
        )
        base_fingerprint = self._fingerprint(
            source_fact_ids=source_fact_ids,
            failed_intent_ids=failed_intent_ids,
            actionable_todo_ids=actionable_todo_ids,
            todo_review_ids=todo_review_ids,
            open_explore_ids=open_explore_ids,
            unassessed_intent_ids=unassessed_intent_ids,
        )
        fingerprint = hashlib.sha256(
            f"continuation:{task.current_goal_cycle_id}:{base_fingerprint}".encode()
        ).hexdigest()
        return self._create_run(
            conn,
            task=task,
            source_fact_ids=source_fact_ids,
            trigger="continuation",
            trigger_payload={
                "actor": actor,
                "requested_at": now,
                "previous_goal": previous_goal,
                "current_goal": task.goal,
                "continuation_reason": continuation_reason,
                "focus": focus,
                "source_fact_ids": source_fact_ids,
                "open_explore_ids": open_explore_ids,
                "failed_intent_ids": failed_intent_ids,
                "actionable_todo_ids": actionable_todo_ids,
                "todo_review_ids": todo_review_ids,
                "unassessed_intent_ids": unassessed_intent_ids,
                "goal_achieved_cancelled_intent_ids": cancelled_intent_ids,
            },
            input_fingerprint=fingerprint,
            now=now,
        )

    def reconcile_task(self, task_id: str) -> str | None:
        now = utcnow()
        with transaction(self.engine) as conn:
            return self._reconcile_task_in(conn, task_id=task_id, now=now)

    def reconcile_all(self) -> ReasonReconcileReport:
        now = utcnow()
        created: list[str] = []
        with transaction(self.engine) as conn:
            task_ids = list(
                conn.execute(
                    select(tasks.c.id)
                    .where(
                        tasks.c.status == "running",
                        tasks.c.legacy_readonly.is_(False),
                        tasks.c.archived.is_(False),
                        or_(
                            tasks.c.run_deadline_at.is_(None),
                            tasks.c.run_deadline_at > now,
                        ),
                    )
                    .order_by(tasks.c.created_at, tasks.c.id)
                ).scalars()
            )
            for task_id in task_ids:
                run_id = self._reconcile_task_in(conn, task_id=task_id, now=now)
                if run_id and run_id not in created:
                    active = self.repository.get_run(conn, task_id, run_id)
                    if active is not None and active.created_at == now:
                        created.append(run_id)
        return ReasonReconcileReport(created_reason_run_ids=created)

    def _reconcile_task_in(self, conn, *, task_id: str, now: str) -> str | None:
        if conn.execute(
            select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
        ).scalar_one_or_none() is None:
            return None
        task = self.tasks.get(conn, task_id)
        if task is None or not task.is_runnable_at(now) or task.legacy_readonly or task.archived:
            return None

        in_flight = self.repository.in_flight_for_task(conn, task_id)
        pending_runs = self.repository.pending_runs_for_task(conn, task_id)
        config = load_runtime_config(conn)
        queue_limit = getattr(config, "reason_queue_limit", 3)

        # 1. 检查是否已有活跃的 ReasonRun
        if in_flight is not None:
            # 当前已有 ReasonRun 正在运行/认领中
            # 只有当触发原因是「真实数据事件」（非瞬态 low_watermark/max_wait），且排队数未达上限时，才允许入队排队
            data_driven_signals = bool(
                state["unplanned_facts"]
                or state["unassessed_intent_ids"]
                or state["failed_intent_ids"]
            )
            if not data_driven_signals:
                return in_flight.id

            if len(pending_runs) >= queue_limit:
                # 已达最大排队数，复用现有的排队槽位
                return pending_runs[0].id

            # 检查是否已有完全相同指纹的 pending 运行
            fingerprint = self._fingerprint(
                source_fact_ids=source_fact_ids,
                failed_intent_ids=failed_intent_ids,
                actionable_todo_ids=actionable_todo_ids,
                todo_review_ids=todo_review_ids,
                open_explore_ids=open_explore_ids,
                unassessed_intent_ids=unassessed_intent_ids,
            )
            if pending_runs and any(r.input_fingerprint == fingerprint for r in pending_runs):
                return pending_runs[0].id

            # 创建排队状态的 ReasonRun（延迟绑定：source_fact_ids 将在 worker 认领时重新刷新为最新全图）
            queued_run = self._create_run(
                conn,
                task=task,
                source_fact_ids=source_fact_ids,
                trigger="fact_batch" if state["unplanned_facts"] else "intent_assessment",
                trigger_payload={
                    "queued": True,
                    "in_flight_run_id": in_flight.id,
                    "unplanned_fact_count": len(source_fact_ids),
                    "source_fact_ids": source_fact_ids,
                },
                input_fingerprint=fingerprint,
                now=now,
            )
            return queued_run.id

        if pending_runs:
            # 当前无 in_flight 但已有 pending 实例在等待调度领取
            return pending_runs[0].id

        bootstrap_completed = conn.execute(
            select(intents.c.id)
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "bootstrap",
                intents.c.status == "completed",
                intents.c.archived.is_(False),
            )
            .limit(1)
        ).scalar_one_or_none()
        if bootstrap_completed is None:
            return None

        state = self._input_state(conn, task_id)
        source_fact_ids = [row["id"] for row in state["unplanned_facts"]]
        open_explore_ids = list(state["open_explore_ids"])
        failed_intent_ids = list(state["failed_intent_ids"])
        actionable_todo_ids = list(state["actionable_todo_ids"])
        todo_review_ids = list(state["todo_review_ids"])
        unassessed_intent_ids = list(state["unassessed_intent_ids"])
        # A terminal Explore without a follow-up is itself a planning signal.
        signals_present = bool(
            source_fact_ids
            or failed_intent_ids
            or actionable_todo_ids
            or todo_review_ids
            or unassessed_intent_ids
        )
        if not signals_present:
            self._set_phase(conn, task_id, "explore" if open_explore_ids else "waiting", now)
            return None

        config = load_runtime_config(conn)
        latest = self.repository.latest_completed_row(conn, task_id)
        oldest_age = (
            max(
                0.0,
                (
                    parse_utc(now)
                    - parse_utc(state["unplanned_facts"][0]["created_at"])
                ).total_seconds(),
            )
            if state["unplanned_facts"]
            else 0.0
        )

        trigger = ""
        if latest is None:
            trigger = "bootstrap"
        else:
            last_finished = latest["concluded_at"] or latest["updated_at"]
            elapsed = max(0.0, (parse_utc(now) - parse_utc(last_finished)).total_seconds())
            # Unassessed terminal Intents are a correctness signal, not a
            # throughput hint.  Do not let the normal Reason debounce leave
            # them waiting (and potentially forgotten) after Explore ends.
            if elapsed < config.reason_min_interval_seconds and not unassessed_intent_ids:
                self._set_phase(conn, task_id, "explore" if open_explore_ids else "waiting", now)
                return None
            if len(source_fact_ids) >= config.reason_batch_size:
                trigger = "fact_batch"
            elif unassessed_intent_ids and not open_explore_ids:
                # Every terminal Explore must receive exactly one Reason-side
                # assessment, even if its Todo was already closed or the
                # dispatcher low-watermark is configured to zero.
                trigger = "intent_assessment"
            elif len(open_explore_ids) < config.reason_low_watermark and signals_present:
                trigger = "low_watermark"
            elif source_fact_ids and oldest_age >= config.reason_max_wait_seconds:
                trigger = "max_wait"
            else:
                self._set_phase(conn, task_id, "explore" if open_explore_ids else "waiting", now)
                return None

        fingerprint = self._fingerprint(
            source_fact_ids=source_fact_ids,
            failed_intent_ids=failed_intent_ids,
            actionable_todo_ids=actionable_todo_ids,
            todo_review_ids=todo_review_ids,
            open_explore_ids=open_explore_ids,
            unassessed_intent_ids=unassessed_intent_ids,
        )
        latest_deduplication = self.repository.latest_deduplication_row(conn, task_id)
        if (
            not unassessed_intent_ids
            and
            latest_deduplication is not None
            and latest_deduplication["input_fingerprint"] == fingerprint
        ):
            self._set_phase(conn, task_id, "explore" if open_explore_ids else "waiting", now)
            return None

        run = self._create_run(
            conn,
            task=task,
            source_fact_ids=source_fact_ids,
            trigger=trigger,
            trigger_payload={
                "unplanned_fact_count": len(source_fact_ids),
                "open_explore_count": len(open_explore_ids),
                "failed_intent_ids": failed_intent_ids,
                "actionable_todo_ids": actionable_todo_ids,
                "todo_review_ids": todo_review_ids,
                "unassessed_intent_ids": unassessed_intent_ids,
                "oldest_fact_age_seconds": oldest_age,
            },
            input_fingerprint=fingerprint,
            now=now,
        )
        self._set_phase(conn, task_id, "reason", now)
        return run.id

    def _create_run(
        self,
        conn,
        *,
        task,
        source_fact_ids: list[str],
        trigger: str,
        trigger_payload: dict,
        input_fingerprint: str,
        now: str,
    ) -> ReasonRun:
        config = load_runtime_config(conn)
        settings_version = conn.execute(
            select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
        ).scalar_one_or_none() or 1
        run = self.repository.create_run(
            conn,
            task_id=task.id,
            goal_cycle_id=task.current_goal_cycle_id,
            source_fact_ids=source_fact_ids,
            trigger=trigger,
            trigger_payload=trigger_payload,
            input_fingerprint=input_fingerprint,
            worker_pool=task.worker_pool,
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
        )
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task.id,
                reason_run_id=run.id,
                event_type="reason_run.created",
                message="已创建 ReasonRun，等待空闲 Worker 自动领取",
                payload={"phase": "reason", "trigger": trigger, **trigger_payload},
            ),
        )
        return run

    def _input_state(self, conn, task_id: str) -> dict[str, object]:
        planned_fact_exists = exists(
            select(reason_run_sources.c.fact_id)
            .join(
                reason_runs,
                (reason_runs.c.task_id == reason_run_sources.c.task_id)
                & (reason_runs.c.id == reason_run_sources.c.reason_run_id),
            )
            .where(
                reason_run_sources.c.task_id == task_id,
                reason_run_sources.c.fact_id == facts.c.id,
                reason_runs.c.status.in_(["pending", "claimed", "running", "concluding", "completed"]),
            )
        )
        unplanned = list(
            conn.execute(
                select(facts.c.id, facts.c.created_at)
                .where(
                    facts.c.task_id == task_id,
                    facts.c.archived.is_(False),
                    facts.c.id.not_in(["origin", "goal"]),
                    ~planned_fact_exists,
                )
                .order_by(facts.c.created_at, facts.c.id)
            ).mappings()
        )
        open_explore_ids = list(
            conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind == "explore",
                    intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                    intents.c.archived.is_(False),
                )
                .order_by(intents.c.created_at, intents.c.id)
            ).scalars()
        )
        failed_intent_ids = list(
            conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind == "explore",
                    intents.c.status.in_(["failed", "cancelled"]),
                    ~(
                        (intents.c.status == "cancelled")
                        & (intents.c.last_failure_kind == "goal_achieved")
                    ),
                    intents.c.archived.is_(False),
                )
                .order_by(intents.c.created_at, intents.c.id)
            ).scalars()
        )
        todo_has_open_explore = exists(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.todo_id == todos.c.id,
                intents.c.kind == "explore",
                intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
            )
        )
        actionable_todo_ids = list(
            conn.execute(
                select(todos.c.id)
                .where(
                    todos.c.task_id == task_id,
                    todos.c.status.in_(["open", "partial", "blocked"]),
                    todos.c.archived.is_(False),
                    ~todo_has_open_explore,
                )
                .order_by(todos.c.priority.desc(), todos.c.id)
            ).scalars()
        )
        return {
            "unplanned_facts": unplanned,
            "open_explore_ids": open_explore_ids,
            "failed_intent_ids": failed_intent_ids,
            "actionable_todo_ids": actionable_todo_ids,
            "todo_review_ids": self._todo_review_ids(conn, task_id),
            "unassessed_intent_ids": self._unassessed_intent_ids(conn, task_id),
        }

    @staticmethod
    def _fingerprint(
        *,
        source_fact_ids: list[str],
        failed_intent_ids: list[str],
        actionable_todo_ids: list[str],
        todo_review_ids: list[str],
        open_explore_ids: list[str],
        unassessed_intent_ids: list[str] | None = None,
    ) -> str:
        payload = json.dumps(
            {
                "facts": sorted(set(source_fact_ids)),
                "failed_intents": sorted(set(failed_intent_ids)),
                "actionable_todos": sorted(set(actionable_todo_ids)),
                "todo_reviews": sorted(set(todo_review_ids)),
                "open_explore": sorted(set(open_explore_ids)),
                "unassessed_intents": sorted(set(unassessed_intent_ids or [])),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _set_phase(conn, task_id: str, phase: str, now: str) -> None:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.status == "running")
            .values(phase=phase, updated_at=now)
        )

    # ------------------------------------------------------------------
    # ReasonRun lifecycle
    # ------------------------------------------------------------------
    def get_run(self, task_id: str, reason_run_id: str) -> ReasonRun:
        with self.engine.connect() as conn:
            run = self.repository.get_run(conn, task_id, reason_run_id)
        if run is None:
            raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
        return run

    def start_run(self, task_id: str, reason_run_id: str, *, lease_id: str) -> StartedReasonRun:
        now = utcnow()
        with transaction(self.engine) as conn:
            run = self._require_run_lease(conn, task_id, reason_run_id, lease_id)
            task = self.tasks.get(conn, task_id)
            if task is None or task.status != "running" or task.archived:
                raise LeaseInactiveError("Task is not running")
            if run.status == "running":
                return StartedReasonRun(
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    worker_name=run.worker or "",
                    lease_id=lease_id,
                )
            if run.status != "claimed":
                raise ConflictError("ReasonRun is not in claimed state")

            # 晚绑定 (Late-Binding)：在正式 start 时，动态从数据库重新拉取此时刻全部未规划的 Fact，
            # 确保即使在排队期间产生的全部新 Fact 也能被 100% 完整吃进当前 ReasonRun
            state = self._input_state(conn, task_id)
            latest_source_fact_ids = [row["id"] for row in state["unplanned_facts"]]
            if latest_source_fact_ids:
                fingerprint = self._fingerprint(
                    source_fact_ids=latest_source_fact_ids,
                    failed_intent_ids=list(state["failed_intent_ids"]),
                    actionable_todo_ids=list(state["actionable_todo_ids"]),
                    todo_review_ids=list(state["todo_review_ids"]),
                    open_explore_ids=list(state["open_explore_ids"]),
                    unassessed_intent_ids=list(state["unassessed_intent_ids"]),
                )
                self.repository.refresh_run_sources(
                    conn,
                    task_id=task_id,
                    run_id=reason_run_id,
                    source_fact_ids=latest_source_fact_ids,
                    input_fingerprint=fingerprint,
                )

            if not self.repository.start(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                lease_id=lease_id,
                now=now,
            ):
                raise ConflictError("ReasonRun start lost a concurrent race")
            self._set_phase(conn, task_id, "reason", now)
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    worker=run.worker or "",
                    event_type="reason_run.started",
                    message="ReasonRun 开始执行",
                    payload={"phase": "reason", "lease_id": lease_id},
                ),
            )
            return StartedReasonRun(
                task_id=task_id,
                reason_run_id=reason_run_id,
                worker_name=run.worker or "",
                lease_id=lease_id,
            )

    def heartbeat_run(self, task_id: str, reason_run_id: str, *, lease_id: str) -> None:
        with transaction(self.engine) as conn:
            if not self.repository.heartbeat(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                lease_id=lease_id,
                now=utcnow(),
            ):
                raise LeaseInactiveError("ReasonRun lease is no longer active")

    def activity_run(
        self,
        task_id: str,
        reason_run_id: str,
        *,
        lease_id: str,
        activity_kind: str,
        activity_summary: str,
        activity_tool: str,
    ) -> None:
        with transaction(self.engine) as conn:
            run = self._require_run_lease(conn, task_id, reason_run_id, lease_id)
            task = self.tasks.get(conn, task_id)
            if task is None or task.status != "running":
                raise LeaseInactiveError("Task is not running")
            if run.status not in {"claimed", "running", "concluding"}:
                raise LeaseInactiveError("ReasonRun lease is not active")
            if not self.repository.update_activity(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                lease_id=lease_id,
                activity_kind=activity_kind,
                activity_summary=activity_summary,
                activity_tool=activity_tool,
                now=utcnow(),
            ):
                raise LeaseInactiveError("ReasonRun lease is no longer active")

    def begin_conclude_run(self, task_id: str, reason_run_id: str, *, lease_id: str) -> None:
        with transaction(self.engine) as conn:
            self._require_run_lease(conn, task_id, reason_run_id, lease_id)
            if not self.repository.begin_conclude(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                lease_id=lease_id,
                now=utcnow(),
            ):
                raise ConflictError("ReasonRun is not running under this lease")

    def release_run(
        self,
        task_id: str,
        reason_run_id: str,
        *,
        lease_id: str,
        failure_kind: str,
        failure_class: str,
    ) -> ReleasedReasonRun:
        now = utcnow()
        with transaction(self.engine) as conn:
            run = self._require_run_lease(conn, task_id, reason_run_id, lease_id)
            config = load_runtime_config(conn)
            next_count = (
                run.retry_count + 1
                if failure_class == "execution"
                else run.infrastructure_retry_count + 1
            )
            limit = (
                config.execution_retry_limit
                if failure_class == "execution"
                else config.infrastructure_retry_limit
            )
            terminal = next_count >= limit
            delay = min(
                config.retry_max_seconds,
                config.retry_base_seconds * (2 ** min(max(0, next_count - 1), 30)),
            )
            next_retry_at = (
                format_utc(parse_utc(now) + timedelta(seconds=delay)) if not terminal else None
            )
            if not self.repository.release(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                lease_id=lease_id,
                failure_kind=failure_kind,
                failure_class=failure_class,
                next_retry_at=next_retry_at,
                terminal=terminal,
                now=now,
            ):
                raise LeaseInactiveError("ReasonRun lease is no longer active")
            self.repository.abandon_open(
                conn,
                task_id=task_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                failure_reason=failure_kind or "reason_run_released",
                now=now,
            )
            task = self.tasks.get(conn, task_id)
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id if task else None,
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    worker=run.worker or "",
                    event_type="reason_run.released",
                    level="warning" if failure_class == "transient" else "info",
                    message=f"ReasonRun 已释放：{failure_kind or '未指定原因'}",
                    payload={
                        "phase": "reason",
                        "failure_kind": failure_kind,
                        "failure_class": failure_class,
                        "terminal": terminal,
                        "next_retry_at": next_retry_at,
                    },
                ),
            )
            if failure_class == "transient" and run.worker:
                cooldown = format_utc(
                    parse_utc(now)
                    + timedelta(seconds=config.worker_failure_cooldown_seconds)
                )
                conn.execute(
                    update(workers)
                    .where(workers.c.name == run.worker)
                    .values(cooldown_until=cooldown, updated_at=now)
                )
            return ReleasedReasonRun(
                task_id=task_id,
                reason_run_id=reason_run_id,
                status="failed" if terminal else "pending",
                next_retry_at=next_retry_at,
                retry_count=run.retry_count + (1 if failure_class == "execution" else 0),
                infrastructure_retry_count=run.infrastructure_retry_count
                + (1 if failure_class == "transient" else 0),
            )

    def _require_run_lease(self, conn, task_id: str, reason_run_id: str, lease_id: str) -> ReasonRun:
        run = self.repository.get_run(conn, task_id, reason_run_id)
        if run is None:
            raise NotFoundError(f"ReasonRun not found: {task_id}/{reason_run_id}")
        if run.lease_id != lease_id:
            raise LeaseInactiveError("ReasonRun lease does not match")
        return run

    # ------------------------------------------------------------------
    # Workspace incremental operations
    # ------------------------------------------------------------------
    def open(
        self,
        *,
        task_id: str,
        reason_run_id: str,
        lease_id: str,
        context_revision: int,
    ) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            run = self._require_run_lease(conn, task_id, reason_run_id, lease_id)
            if run.status not in {"running", "concluding"}:
                raise ConflictError("ReasonRun is not active")
            context_exists = conn.execute(
                select(context_revisions.c.revision).where(
                    context_revisions.c.task_id == task_id,
                    context_revisions.c.revision == context_revision,
                    context_revisions.c.reason_run_id == reason_run_id,
                    context_revisions.c.lease_id == lease_id,
                    context_revisions.c.phase == "reason",
                )
            ).scalar_one_or_none()
            if context_exists is None:
                raise ConflictError("Reason Context does not match this lease")
            existing = self.repository.for_attempt(conn, task_id, reason_run_id, lease_id)
            if existing is not None:
                if existing.status == "open" and existing.worker == (run.worker or ""):
                    return existing
                raise ConflictError("Reason attempt already has a closed Workspace")
            workspace = self.repository.open(
                conn,
                task_id=task_id,
                reason_run_id=reason_run_id,
                lease_id=lease_id,
                worker=run.worker or "",
                context_revision=context_revision,
                now=utcnow(),
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    workspace_id=workspace.id,
                    worker=run.worker or "",
                    event_type="reason.workspace_opened",
                    message="Reason 已打开规划工作区",
                    payload={"phase": "reason", "workspace_id": workspace.id},
                ),
            )
            return workspace

    def get(self, workspace_id: str) -> ReasonWorkspace:
        with self.engine.connect() as conn:
            workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
        return workspace

    def abandon(self, workspace_id: str, *, lease_id: str, failure_reason: str) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            abandoned = self.repository.abandon(
                conn,
                workspace_id,
                failure_reason=failure_reason.strip() or "Reason execution failed",
                now=utcnow(),
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    task_id=workspace.task_id,
                    reason_run_id=workspace.reason_run_id,
                    workspace_id=workspace.id,
                    worker=workspace.worker,
                    event_type="reason.workspace_abandoned",
                    level="warning",
                    message=f"Reason 规划未提交：{failure_reason.strip() or '执行失败'}",
                    payload={
                        "phase": "reason",
                        "workspace_id": workspace_id,
                        "failure_reason": failure_reason,
                    },
                ),
            )
            return abandoned

    def add_todo(self, workspace_id: str, todo: DraftTodo, *, lease_id: str) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            self._validate_sources(conn, workspace.task_id, todo.sources)
            existing = next(
                (item for item in workspace.draft.todo_adds if item.ref == todo.ref),
                None,
            )
            if existing is not None:
                if existing == todo:
                    return workspace
                raise ConflictError(f"Todo draft ref already exists with different content: {todo.ref}")
            draft = workspace.draft.model_copy(
                update={"todo_adds": [*workspace.draft.todo_adds, todo]}
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def update_todo(
        self,
        workspace_id: str,
        update_command: DraftTodoUpdate,
        *,
        lease_id: str,
    ) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            self._validate_sources(conn, workspace.task_id, update_command.source_adds)
            is_draft_ref = any(
                item.ref == update_command.todo_id for item in workspace.draft.todo_adds
            )
            if not is_draft_ref and self.todos.get(
                conn, workspace.task_id, update_command.todo_id
            ) is None:
                raise NotFoundError(f"Todo not found: {update_command.todo_id}")
            updates = [
                item
                for item in workspace.draft.todo_updates
                if item.todo_id != update_command.todo_id
            ]
            previous = next(
                (
                    item
                    for item in workspace.draft.todo_updates
                    if item.todo_id == update_command.todo_id
                ),
                None,
            )
            if previous is not None:
                merged = previous.model_dump()
                merged.update(update_command.model_dump(exclude_unset=True))
                merged["source_adds"] = [
                    source.model_dump(mode="python")
                    for source in {
                        (source.type, source.id): source
                        for source in [*previous.source_adds, *update_command.source_adds]
                    }.values()
                ]
                update_command = DraftTodoUpdate.model_validate(merged)
            updates.append(update_command)
            draft = workspace.draft.model_copy(update={"todo_updates": updates})
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def delete_todo(
        self,
        workspace_id: str,
        delete_command: DraftTodoDelete,
        *,
        lease_id: str,
    ) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            target_id = delete_command.todo_id
            # 1. 如果是本 Workspace 新增的草稿，直接从 todo_adds 中移除
            todo_adds = [item for item in workspace.draft.todo_adds if item.ref != target_id]
            # 2. 从 todo_updates 中也移除针对该 Todo 的修改
            todo_updates = [item for item in workspace.draft.todo_updates if item.todo_id != target_id]
            # 3. 检查数据库中是否存在已持久化的 Todo
            is_existing = self.todos.get(conn, workspace.task_id, target_id) is not None
            deletions = [item for item in workspace.draft.todo_deletions if item.todo_id != target_id]
            if is_existing:
                deletions.append(delete_command)
            draft = workspace.draft.model_copy(
                update={
                    "todo_adds": todo_adds,
                    "todo_updates": todo_updates,
                    "todo_deletions": deletions,
                }
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def propose_intent(
        self,
        workspace_id: str,
        proposal: DraftIntent,
        *,
        lease_id: str,
    ) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            run = self._workspace_run(conn, workspace)
            self._validate_intent(conn, workspace.task_id, workspace.draft, proposal, run)
            identity = self._intent_identity(proposal)
            existing = next(
                (
                    item
                    for item in workspace.draft.intent_proposals
                    if self._intent_identity(item) == identity
                ),
                None,
            )
            if existing is not None:
                if existing == proposal:
                    return workspace
                raise ConflictError("Intent proposal already exists with different parameters")
            if len(workspace.draft.intent_proposals) >= MAX_WORKSPACE_INTENTS:
                raise ConflictError(
                    f"Reason Workspace Intent infrastructure limit reached: {MAX_WORKSPACE_INTENTS}"
                )
            draft = workspace.draft.model_copy(
                update={"intent_proposals": [*workspace.draft.intent_proposals, proposal]}
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def assess_intent(
        self,
        workspace_id: str,
        assessment: DraftIntentAssessment,
        *,
        lease_id: str,
    ) -> ReasonWorkspace:
        """Stage a one-time evaluation for a terminal Explore Intent.

        Reason deliberately records the evaluation in its Workspace first. The
        actual Intent row is updated atomically with the rest of the plan at
        commit time, so a failed/abandoned Reason run cannot leave a half-written
        assessment behind.
        """

        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            self._validate_intent_assessment(
                conn, workspace.task_id, workspace.draft, assessment
            )
            existing = next(
                (
                    item
                    for item in workspace.draft.intent_assessments
                    if item.intent_id == assessment.intent_id
                ),
                None,
            )
            if existing is not None:
                if existing == assessment:
                    return workspace
                raise ConflictError(
                    "Intent assessment already exists with different parameters"
                )
            if len(workspace.draft.intent_assessments) >= MAX_WORKSPACE_INTENTS:
                raise ConflictError(
                    f"Reason Workspace Intent assessment infrastructure limit reached: {MAX_WORKSPACE_INTENTS}"
                )
            draft = workspace.draft.model_copy(
                update={
                    "intent_assessments": [
                        *workspace.draft.intent_assessments,
                        assessment,
                    ]
                }
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def assess_goal(
        self,
        workspace_id: str,
        assessment: GoalAssessment,
        *,
        lease_id: str,
    ) -> ReasonWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._require_active_open(conn, workspace_id, lease_id)
            self._validate_goal_assessment(conn, workspace, assessment)
            if workspace.draft.goal_assessment == assessment:
                return workspace
            draft = workspace.draft.model_copy(update={"goal_assessment": assessment})
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def status(self, workspace_id: str, *, lease_id: str) -> WorkspaceStatus:
        with self.engine.connect() as conn:
            workspace = self.repository.get(conn, workspace_id)
            if workspace is None:
                raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
            if workspace.status == "open":
                workspace = self._require_active_open(conn, workspace_id, lease_id)
            else:
                self._verify_workspace_identity(conn, workspace, lease_id)
            run = self._workspace_run(conn, workspace)
            errors = [] if workspace.status == "committed" else self._readiness_errors(
                conn, workspace, run
            )
            open_count, limit, remaining = self._intent_quota(conn, workspace, run)
            assessment = workspace.draft.goal_assessment
            todo_review_ids = self._run_todo_review_ids(conn, workspace.task_id, run)
            return WorkspaceStatus(
                workspace_id=workspace.id,
                status=workspace.status,
                source_fact_ids=run.source_fact_ids,
                todo_add_count=len(workspace.draft.todo_adds),
                todo_update_count=len(workspace.draft.todo_updates),
                intent_proposal_count=len(workspace.draft.intent_proposals),
                intent_assessment_count=len(workspace.draft.intent_assessments),
                todo_refs=[item.ref for item in workspace.draft.todo_adds],
                updated_todo_ids=[item.todo_id for item in workspace.draft.todo_updates],
                todo_review_ids=todo_review_ids,
                goal_assessed=assessment is not None,
                goal_achieved=bool(assessment and assessment.achieved),
                open_explore_intents=open_count,
                proposal_limit=limit,
                remaining_intent_quota=remaining,
                ready=workspace.status == "committed" or not errors,
                errors=errors,
            )

    def commit(self, workspace_id: str, *, lease_id: str) -> CommitResult:
        with transaction(self.engine) as conn:
            workspace = self.repository.get(conn, workspace_id)
            if workspace is None:
                raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
            self._verify_workspace_identity(conn, workspace, lease_id)
            if workspace.status == "committed":
                todo_ids, intent_ids = self.repository.commit_result(conn, workspace)
                result = CommitResult(
                    workspace=workspace,
                    created_todo_ids=todo_ids,
                    created_intent_ids=intent_ids,
                )
            else:
                workspace = self._require_active_open(conn, workspace_id, lease_id)
                run = self._workspace_run(conn, workspace)
                errors = self._readiness_errors(conn, workspace, run)
                if errors:
                    raise ConflictError("Reason Workspace 未就绪：" + "；".join(errors))
                draft = workspace.draft
                assessment = draft.goal_assessment
                assert assessment is not None
                task = self.tasks.get(conn, workspace.task_id)
                assert task is not None
                now = utcnow()
                todo_refs: dict[str, str] = {}
                created_todo_ids: list[str] = []

                for todo in draft.todo_adds:
                    staged_update = next(
                        (item for item in draft.todo_updates if item.todo_id == todo.ref),
                        None,
                    )
                    sources = list(todo.sources)
                    if staged_update is not None:
                        sources.extend(staged_update.source_adds)
                    unique_sources = {(source.type, source.id): source for source in sources}
                    created = self.todos.create(
                        conn,
                        task_id=workspace.task_id,
                        command=CreateTodo(
                            title=(
                                staged_update.title
                                if staged_update and staged_update.title
                                else todo.title
                            ),
                            category=(
                                staged_update.category
                                if staged_update and staged_update.category
                                else todo.category
                            ),
                            theme=(
                                staged_update.theme
                                if staged_update and staged_update.theme is not None
                                else todo.theme
                            ),
                            priority=(
                                staged_update.priority
                                if staged_update and staged_update.priority is not None
                                else todo.priority
                            ),
                            status=(
                                staged_update.status
                                if staged_update and staged_update.status is not None
                                else todo.status
                            ),
                            status_reason=(
                                staged_update.status_reason
                                if staged_update and staged_update.status_reason is not None
                                else todo.status_reason
                            ),
                            sources=list(unique_sources.values()),
                            actor=workspace.worker,
                        ),
                        now=now,
                    )
                    todo_refs[todo.ref] = created.id
                    created_todo_ids.append(created.id)

                evidence_refs = [
                    f"todo:{todo_refs[item_id]}"
                    if kind == "todo" and item_id in todo_refs
                    else ref
                    for ref in assessment.evidence_refs
                    for kind, _separator, item_id in [ref.partition(":")]
                ]
                assessment = assessment.model_copy(
                    update={"evidence_refs": list(dict.fromkeys(evidence_refs))}
                )
                GoalService.validate_evidence(
                    conn,
                    workspace.task_id,
                    assessment.evidence_refs,
                    achieved=assessment.achieved,
                )

                for update_command in draft.todo_updates:
                    if update_command.todo_id in todo_refs:
                        continue
                    values = update_command.model_dump(
                        exclude_none=True,
                        exclude={"todo_id", "source_adds"},
                    )
                    if update_command.status == "completed":
                        values["completed_at"] = now
                    elif update_command.status is not None:
                        values["completed_at"] = None
                    values["updated_at"] = now
                    self.todos.update(conn, workspace.task_id, update_command.todo_id, values)
                    self.todos.add_sources(
                        conn,
                        task_id=workspace.task_id,
                        todo_id=update_command.todo_id,
                        sources=update_command.source_adds,
                    )

                # 执行已持久化 Todo 的归档/软删除
                for delete_command in draft.todo_deletions:
                    if delete_command.todo_id in todo_refs:
                        continue
                    self.todos.update(
                        conn,
                        workspace.task_id,
                        delete_command.todo_id,
                        {
                            "archived": True,
                            "status": "cancelled",
                            "status_reason": delete_command.reason,
                            "updated_at": now,
                        },
                    )

                # Apply one-time evaluations to already-dispatched terminal
                # Intents before creating new proposals.  Both operations are
                # part of this transaction, so a failed validation rolls back
                # the complete Reason plan.
                for intent_assessment in draft.intent_assessments:
                    self._commit_intent_assessment(
                        conn,
                        task_id=workspace.task_id,
                        assessment=intent_assessment,
                        now=now,
                    )

                # Align the task intent counter with the real table before
                # handing out ids.  A rolled-back transaction (e.g. a prior
                # commit that failed on a unique violation) leaves the
                # counter behind the table; re-using an existing id would
                # then wedge every later commit in an infinite
                # control_plane_unavailable retry loop.
                self._align_intent_counter(conn, workspace.task_id)

                created_intent_ids: list[str] = []
                for proposal in draft.intent_proposals:
                    todo_id = proposal.todo_id or todo_refs.get(proposal.todo_ref or "")
                    if todo_id is None:
                        raise ConflictError(f"Intent references unknown Todo: {proposal.todo_ref}")
                    intent_id = allocate_id(
                        conn, scope=workspace.task_id, kind="intent", prefix="i"
                    )
                    intent_values = {
                        "id": intent_id,
                        "task_id": workspace.task_id,
                        "goal_cycle_id": run.goal_cycle_id,
                        "todo_id": todo_id,
                        "kind": "explore",
                        "description": proposal.description,
                        "status": "pending",
                        "creator": workspace.worker,
                        "worker": None,
                        "worker_pool_json": json.dumps(task.worker_pool, ensure_ascii=False),
                        "targets_json": json.dumps(proposal.targets, ensure_ascii=False),
                        "execute_timeout_seconds": proposal.execute_seconds,
                        "conclude_timeout_seconds": proposal.conclude_seconds,
                        "retry_count": 0,
                        "infrastructure_retry_count": 0,
                        "last_failure_kind": "",
                        "failure_class": "",
                        "created_at": now,
                        "archived": False,
                    }
                    # These columns are added by the Intent lifecycle
                    # migration.  Keeping the guards makes old read-only
                    # SQLite fixtures importable during a rolling upgrade.
                    if intents.c.get("priority") is not None:
                        intent_values["priority"] = proposal.priority
                    if intents.c.get("assessment_status") is not None:
                        intent_values["assessment_status"] = "open"
                    if intents.c.get("assessment_reason") is not None:
                        intent_values["assessment_reason"] = ""
                    if intents.c.get("assessed_at") is not None:
                        intent_values["assessed_at"] = None
                    conn.execute(intents.insert().values(**intent_values))
                    conn.execute(
                        intent_sources.insert(),
                        [
                            {
                                "task_id": workspace.task_id,
                                "intent_id": intent_id,
                                "fact_id": fact_id,
                            }
                            for fact_id in proposal.from_facts
                        ],
                    )
                    created_intent_ids.append(intent_id)

                if not self.repository.complete(
                    conn,
                    task_id=workspace.task_id,
                    run_id=run.id,
                    lease_id=lease_id,
                    now=now,
                ):
                    raise ConflictError("ReasonRun completion lost a concurrent race")
                self.goals.create(
                    conn,
                    task_id=workspace.task_id,
                    goal_cycle_id=run.goal_cycle_id,
                    assessment=assessment,
                    evaluator=workspace.worker,
                    reason_run_id=run.id,
                    workspace_id=workspace.id,
                    now=now,
                )
                if assessment.achieved:
                    GoalService.complete_task(
                        conn,
                        workspace.task_id,
                        now=now,
                        exclude_workspace_id=workspace.id,
                        goal_cycle_id=run.goal_cycle_id,
                    )
                else:
                    open_count = self._open_explore_count(conn, workspace.task_id)
                    conn.execute(
                        update(tasks)
                        .where(tasks.c.id == workspace.task_id)
                        .values(
                            phase="explore" if open_count else "waiting",
                            updated_at=now,
                        )
                    )
                committed = self.repository.mark_committed(
                    conn,
                    workspace_id,
                    created_todo_ids=created_todo_ids,
                    created_intent_ids=created_intent_ids,
                    now=now,
                )
                self.audit.record_in(
                    conn,
                    RecordAuditEvent(
                        project_id=task.project_id,
                        task_id=workspace.task_id,
                        reason_run_id=run.id,
                        workspace_id=workspace.id,
                        worker=workspace.worker,
                        event_type="reason.committed",
                        message=(
                            f"Reason 已提交规划：新增 {len(created_intent_ids)} 个 Intent、"
                            f"{len(created_todo_ids)} 个 Todo"
                        ),
                        payload={
                            "phase": "reason",
                            "created_todo_ids": created_todo_ids,
                            "created_intent_ids": created_intent_ids,
                            "goal_achieved": assessment.achieved,
                        },
                    ),
                )
                result = CommitResult(
                    workspace=committed,
                    created_todo_ids=created_todo_ids,
                    created_intent_ids=created_intent_ids,
                )
        self._write_plan_audit(result)
        return result

    def _require_active_open(
        self,
        conn,
        workspace_id: str,
        lease_id: str,
    ) -> ReasonWorkspace:
        workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Reason Workspace not found: {workspace_id}")
        self._verify_workspace_identity(conn, workspace, lease_id)
        if workspace.status != "open":
            raise ConflictError("Reason Workspace is not open")
        run = self._workspace_run(conn, workspace)
        if run.status not in {"running", "concluding"}:
            raise LeaseInactiveError("ReasonRun is not active")
        task = self.tasks.get(conn, workspace.task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {workspace.task_id}")
        if task.status != "running":
            raise LeaseInactiveError("Task is not writable")
        if task.legacy_readonly:
            raise ConflictError("Task is not writable")
        return workspace

    def _verify_workspace_identity(
        self, conn, workspace: ReasonWorkspace, lease_id: str
    ) -> None:
        if workspace.lease_id != lease_id:
            raise LeaseInactiveError("Reason Workspace lease does not match")
        if not workspace.reason_run_id:
            raise ConflictError("Legacy Reason Workspace is read-only")
        run = self.repository.get_run(conn, workspace.task_id, workspace.reason_run_id)
        if run is None:
            raise NotFoundError(
                f"ReasonRun not found: {workspace.task_id}/{workspace.reason_run_id}"
            )
        if run.lease_id != lease_id:
            raise LeaseInactiveError("ReasonRun lease does not match")

    def _workspace_run(self, conn, workspace: ReasonWorkspace) -> ReasonRun:
        if not workspace.reason_run_id:
            raise ConflictError("Legacy Reason Workspace is read-only")
        run = self.repository.get_run(conn, workspace.task_id, workspace.reason_run_id)
        if run is None:
            raise NotFoundError(
                f"ReasonRun not found: {workspace.task_id}/{workspace.reason_run_id}"
            )
        return run

    def _readiness_errors(
        self, conn, workspace: ReasonWorkspace, run: ReasonRun
    ) -> list[str]:
        errors: list[str] = []
        draft = workspace.draft
        refs = [item.ref for item in draft.todo_adds]
        if len(refs) != len(set(refs)):
            errors.append("存在重复的 Todo 临时 ref")
        for item in draft.todo_adds:
            try:
                self._validate_sources(conn, workspace.task_id, item.sources)
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
        for item in draft.todo_updates:
            try:
                self._validate_sources(conn, workspace.task_id, item.source_adds)
                if item.todo_id not in refs and self.todos.get(
                    conn, workspace.task_id, item.todo_id
                ) is None:
                    raise NotFoundError(f"Todo not found: {item.todo_id}")
                persisted = self.todos.get(conn, workspace.task_id, item.todo_id)
                if persisted is not None and persisted.archived:
                    raise ConflictError(f"Todo is archived: {item.todo_id}")
                draft_todo = next(
                    (todo for todo in draft.todo_adds if todo.ref == item.todo_id),
                    None,
                )
                baseline_status = (
                    draft_todo.status if draft_todo is not None else persisted.status
                    if persisted is not None
                    else "open"
                )
                effective_status = item.status or baseline_status
                effective_reason = (
                    item.status_reason
                    if item.status_reason is not None
                    else draft_todo.status_reason
                    if draft_todo is not None
                    else persisted.status_reason
                    if persisted is not None
                    else ""
                )
                if effective_status in TODO_STATUS_REASON_REQUIRED and not effective_reason.strip():
                    raise ConflictError(
                        f"status_reason is required for Todo status {effective_status}"
                    )
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
        for proposal in draft.intent_proposals:
            try:
                self._validate_intent(conn, workspace.task_id, draft, proposal, run)
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
        for assessment in draft.intent_assessments:
            try:
                self._validate_intent_assessment(
                    conn, workspace.task_id, draft, assessment
                )
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
        review_ids = self._run_todo_review_ids(conn, workspace.task_id, run)
        updates_by_id = {item.todo_id: item for item in draft.todo_updates}
        for todo_id in review_ids:
            staged = updates_by_id.get(todo_id)
            if staged is None or staged.status is None:
                errors.append(f"Todo {todo_id} 已无开放 Intent，必须明确更新状态")
                continue
            if staged.status in {"partial", "blocked", "completed"} and not (
                staged.status_reason or ""
            ).strip():
                errors.append(
                    f"Todo {todo_id} 状态为 {staged.status} 时必须填写 status_reason"
                )
            elif staged.status not in {
                "open",
                "partial",
                "blocked",
                "completed",
            }:
                errors.append(f"Todo {todo_id} 使用了未知状态：{staged.status}")
        unconsumed_fact_ids = self._unconsumed_source_fact_ids(workspace, run)
        if unconsumed_fact_ids:
            errors.append(
                "本轮新 Fact 尚未纳入规划或 Goal 评估："
                + ", ".join(unconsumed_fact_ids)
            )
        assessment = draft.goal_assessment
        if assessment is None:
            errors.append("缺少 Goal assessment")
        else:
            try:
                self._validate_goal_assessment(conn, workspace, assessment)
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
            if assessment.achieved and draft.intent_proposals:
                errors.append("Goal 已达成时不能继续派发 Intent")
            open_count = self._open_explore_count(conn, workspace.task_id)
            if not assessment.achieved and not draft.intent_proposals and not open_count:
                unresolved_todos = self._no_op_unresolved_todos(conn, workspace, run)
                if unresolved_todos:
                    errors.append(
                        "本轮不派发 Intent 时，必须先收口无后续工作的 Todo："
                        + ", ".join(unresolved_todos)
                    )
        return list(dict.fromkeys(errors))

    def _no_op_unresolved_todos(
        self,
        conn,
        workspace: ReasonWorkspace,
        run: ReasonRun,
    ) -> list[str]:
        updates = {item.todo_id: item for item in workspace.draft.todo_updates}
        unresolved: list[str] = []

        actionable_ids = run.trigger_payload.get("actionable_todo_ids", [])
        if isinstance(actionable_ids, list):
            for todo_id in actionable_ids:
                if not isinstance(todo_id, str):
                    continue
                todo = self.todos.get(conn, workspace.task_id, todo_id)
                if todo is None or todo.archived or todo.status not in {"open", "partial", "blocked"}:
                    continue
                staged = updates.get(todo_id)
                effective_status = staged.status if staged and staged.status else todo.status
                if effective_status in {"open", "partial"}:
                    unresolved.append(todo_id)

        for todo in workspace.draft.todo_adds:
            staged = updates.get(todo.ref)
            effective_status = staged.status if staged and staged.status else todo.status
            if effective_status in {"open", "partial"}:
                unresolved.append(todo.ref)

        return list(dict.fromkeys(unresolved))

    @staticmethod
    def _unconsumed_source_fact_ids(
        workspace: ReasonWorkspace,
        run: ReasonRun,
    ) -> list[str]:
        consumed: set[str] = set()
        for todo in workspace.draft.todo_adds:
            consumed.update(
                source.id for source in todo.sources if source.type == "fact"
            )
        for update_command in workspace.draft.todo_updates:
            consumed.update(
                source.id
                for source in update_command.source_adds
                if source.type == "fact"
            )
        for proposal in workspace.draft.intent_proposals:
            consumed.update(proposal.from_facts)
        assessment = workspace.draft.goal_assessment
        if assessment is not None:
            consumed.update(
                item_id
                for ref in assessment.evidence_refs
                for kind, separator, item_id in [ref.partition(":")]
                if separator and kind == "fact" and item_id
            )
        return [fact_id for fact_id in run.source_fact_ids if fact_id not in consumed]

    @staticmethod
    def _todo_review_ids(conn, task_id: str) -> list[str]:
        open_explore_exists = exists(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.todo_id == todos.c.id,
                intents.c.kind == "explore",
                intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
            )
        )
        terminal_explore_exists = exists(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.todo_id == todos.c.id,
                intents.c.kind == "explore",
                intents.c.status.in_(TERMINAL_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
            )
        )
        return list(
            conn.execute(
                select(todos.c.id)
                .where(
                    todos.c.task_id == task_id,
                    todos.c.status.in_(["open", "partial", "blocked"]),
                    todos.c.archived.is_(False),
                    terminal_explore_exists,
                    ~open_explore_exists,
                )
                .order_by(todos.c.priority.desc(), todos.c.id)
            ).scalars()
        )

    @staticmethod
    def _unassessed_intent_ids(conn, task_id: str) -> list[str]:
        """Return terminal Explore Intents still awaiting one-time evaluation."""

        assessment_status = intents.c.get("assessment_status")
        if assessment_status is None:
            # A pre-assessment schema cannot participate in the new lifecycle;
            # leave its historical rows untouched until the migration runs.
            return []
        return list(
            conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind == "explore",
                    intents.c.status.in_(TERMINAL_EXPLORE_STATUSES),
                    assessment_status == "open",
                    intents.c.archived.is_(False),
                )
                .order_by(intents.c.created_at, intents.c.id)
            ).scalars()
        )

    def _run_todo_review_ids(self, conn, task_id: str, run: ReasonRun) -> list[str]:
        snapshot = run.trigger_payload.get("todo_review_ids", [])
        if not isinstance(snapshot, list):
            return []
        current = set(self._todo_review_ids(conn, task_id))
        return [todo_id for todo_id in snapshot if isinstance(todo_id, str) and todo_id in current]

    def _intent_quota(
        self, conn, workspace: ReasonWorkspace, run: ReasonRun
    ) -> tuple[int, int, int]:
        open_count = self._open_explore_count(conn, workspace.task_id)
        # Dispatcher concurrency is deliberately outside Reason's concern.
        # Keep these response fields for protocol compatibility, but expose
        # only the workspace infrastructure guard as available capacity.
        limit = MAX_WORKSPACE_INTENTS
        remaining = max(0, limit - len(workspace.draft.intent_proposals))
        return open_count, limit, remaining

    @staticmethod
    def _open_explore_count(conn, task_id: str) -> int:
        return int(
            conn.execute(
                select(func.count())
                .select_from(intents)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind == "explore",
                    intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                    intents.c.archived.is_(False),
                )
            ).scalar_one()
        )

    def _validate_goal_assessment(
        self,
        conn,
        workspace: ReasonWorkspace,
        assessment: GoalAssessment,
    ) -> None:
        draft_refs = {item.ref for item in workspace.draft.todo_adds}
        persisted_refs = [
            ref
            for ref in assessment.evidence_refs
            for kind, separator, item_id in [ref.partition(":")]
            if not (separator and kind == "todo" and item_id in draft_refs)
        ]
        GoalService.validate_evidence(
            conn,
            workspace.task_id,
            persisted_refs,
            achieved=assessment.achieved,
        )

    def _write_plan_audit(self, result: CommitResult) -> None:
        if self.store is None:
            return
        workspace = result.workspace
        if not workspace.reason_run_id:
            return
        run_dir = self.store.run_directory(
            task_id=workspace.task_id,
            reason_run_id=workspace.reason_run_id,
            lease_id=workspace.lease_id,
        )
        path = run_dir / "reason_plan.json"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = {
            "workspace_id": workspace.id,
            "task_id": workspace.task_id,
            "reason_run_id": workspace.reason_run_id,
            "lease_id": workspace.lease_id,
            "committed_at": workspace.committed_at,
            "draft": workspace.draft.model_dump(mode="json"),
            "created_todo_ids": result.created_todo_ids,
            "created_intent_ids": result.created_intent_ids,
        }
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            logger.exception("failed to write Reason audit plan: %s", workspace.id)

    def _validate_sources(self, conn, task_id: str, sources: list[TodoSource]) -> None:
        for source in sources:
            if source.type == "fact" and source.id == "goal":
                raise ConflictError("Goal Fact cannot be used as a Todo source")
            if not self.todos.source_exists(conn, task_id=task_id, source=source):
                raise ConflictError(f"Todo source not found: {source.type}:{source.id}")

    def _validate_intent_assessment(
        self,
        conn,
        task_id: str,
        draft: WorkspaceDraft,
        assessment: DraftIntentAssessment,
    ) -> None:
        if assessment.status == "open":
            raise ConflictError(
                "Intent assessment status open means the Intent has not been assessed yet"
            )
        if assessment.status not in {"partial", "blocked", "completed"}:
            raise ConflictError(f"Unknown Intent assessment status: {assessment.status}")
        if any(
            intents.c.get(name) is None
            for name in ("assessment_status", "assessment_reason", "assessed_at")
        ):
            raise ConflictError(
                "Intent assessment requires the Intent assessment migration"
            )

        row = conn.execute(
            select(intents).where(
                intents.c.task_id == task_id,
                intents.c.id == assessment.intent_id,
            )
        ).mappings().one_or_none()
        if row is None:
            raise NotFoundError(f"Intent not found: {assessment.intent_id}")
        if row["kind"] != "explore":
            raise ConflictError("Only Explore Intent can be assessed")
        if row["archived"]:
            raise ConflictError(f"Intent is archived: {assessment.intent_id}")
        if row["status"] not in TERMINAL_EXPLORE_STATUSES:
            raise ConflictError(
                "Intent assessment requires a terminal execution status: "
                f"{assessment.intent_id} is {row['status']}"
            )
        persisted_status = (
            row.get("assessment_status")
            if intents.c.get("assessment_status") is not None
            else "open"
        ) or "open"
        if persisted_status != "open":
            raise ConflictError(
                f"Intent {assessment.intent_id} has already been assessed: {persisted_status}"
            )
        staged = next(
            (
                item
                for item in draft.intent_assessments
                if item.intent_id == assessment.intent_id
            ),
            None,
        )
        if staged is not None and staged != assessment:
            raise ConflictError(
                "Intent assessment already exists with different parameters"
            )

    @staticmethod
    def _align_intent_counter(conn, task_id: str) -> None:
        """Bring the task intent counter at least past the highest existing id.

        A rolled-back Reason commit (e.g. a unique violation raised while
        inserting a proposed Intent) rolls back the atomic id-counter
        upsert with it, leaving ``id_counters.next_value`` behind the real
        table.  The next commit then re-allocates an id that already exists,
        fails again, and wedges the ReasonRun in an infinite
        ``control_plane_unavailable`` retry loop.  Aligning here makes every
        commit self-healing.
        """
        rows = conn.execute(
            select(intents.c.id).where(intents.c.task_id == task_id)
        ).scalars().all()
        max_num = 0
        for rid in rows:
            if rid.startswith("i") and rid[1:].isdigit():
                max_num = max(max_num, int(rid[1:]))
        conn.execute(
            update(id_counters)
            .where(
                id_counters.c.scope == task_id,
                id_counters.c.kind == "intent",
            )
            .values(next_value=func.greatest(id_counters.c.next_value, max_num + 1))
        )

    @staticmethod
    def _commit_intent_assessment(
        conn,
        *,
        task_id: str,
        assessment: DraftIntentAssessment,
        now: str,
    ) -> None:
        status_column = intents.c.get("assessment_status")
        reason_column = intents.c.get("assessment_reason")
        assessed_at_column = intents.c.get("assessed_at")
        if status_column is None or reason_column is None or assessed_at_column is None:
            # Legacy databases cannot persist the new lifecycle fields.  They
            # are read-only for this feature until the Intent migration lands.
            raise ConflictError(
                "Intent assessment requires the Intent assessment migration"
            )
        result = conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task_id,
                intents.c.id == assessment.intent_id,
                intents.c.kind == "explore",
                intents.c.status.in_(TERMINAL_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
                status_column == "open",
            )
            .values(
                assessment_status=assessment.status,
                assessment_reason=assessment.reason,
                assessed_at=now,
            )
        )
        if not result.rowcount:
            raise ConflictError(
                f"Intent {assessment.intent_id} was assessed concurrently or is no longer terminal"
            )

    def _validate_intent(
        self,
        conn,
        task_id: str,
        draft: WorkspaceDraft,
        proposal: DraftIntent,
        run: ReasonRun,
    ) -> None:
        if proposal.todo_ref:
            todo = next(
                (item for item in draft.todo_adds if item.ref == proposal.todo_ref),
                None,
            )
            if todo is None:
                raise ConflictError(f"Intent references unknown Todo draft: {proposal.todo_ref}")
            todo_id = proposal.todo_ref
        else:
            todo = self.todos.get(conn, task_id, proposal.todo_id or "")
            if todo is None:
                raise NotFoundError(f"Todo not found: {proposal.todo_id}")
            if todo.archived:
                raise ConflictError(f"Intent cannot reference archived Todo: {todo.id}")
            todo_id = proposal.todo_id or ""
        staged = next(
            (item for item in draft.todo_updates if item.todo_id == todo_id),
            None,
        )
        effective_status = staged.status if staged and staged.status is not None else todo.status
        if effective_status in {"blocked", "completed", "rejected", "obsolete"}:
            raise ConflictError(f"Intent cannot reference terminal Todo: {todo_id}")
        if "goal" in proposal.from_facts:
            raise ConflictError("Goal Fact cannot be used as an Intent source")
        for fact_id in proposal.from_facts:
            exists_fact = conn.execute(
                select(facts.c.id).where(
                    facts.c.task_id == task_id,
                    facts.c.id == fact_id,
                    facts.c.archived.is_(False),
                )
            ).scalar_one_or_none()
            if exists_fact is None:
                raise ConflictError(f"Intent source Fact not found: {fact_id}")
        if not (
            run.explore_execute_min_seconds
            <= proposal.execute_seconds
            <= run.explore_execute_max_seconds
        ):
            raise ConflictError(
                "Intent execute_seconds outside configured range: "
                f"{run.explore_execute_min_seconds}-"
                f"{run.explore_execute_max_seconds}"
            )
        if not (
            run.explore_conclude_min_seconds
            <= proposal.conclude_seconds
            <= run.explore_conclude_max_seconds
        ):
            raise ConflictError(
                "Intent conclude_seconds outside configured range: "
                f"{run.explore_conclude_min_seconds}-"
                f"{run.explore_conclude_max_seconds}"
            )
        task = self.tasks.get(conn, task_id)
        assert task is not None
        if task.scope.enabled and not proposal.targets:
            raise ConflictError("Intent targets are required when Task Scope is configured")
        for target in proposal.targets:
            if not self._target_allowed(task, target):
                raise ConflictError(f"Intent target outside Task Scope: {target}")

    @staticmethod
    def _intent_identity(proposal: DraftIntent) -> tuple:
        return (
            proposal.todo_ref or proposal.todo_id,
            proposal.description,
            tuple(proposal.targets),
        )

    @staticmethod
    def _target_allowed(task, target: str) -> bool:
        scope = task.scope
        if not scope.enabled:
            return True
        try:
            candidate = parse_intent_network_target(target)
        except Exception:
            return False
        if candidate is None:
            return True
        host = candidate.host
        if any(scope_entry_matches_host(item, host) for item in scope.exclude):
            return False
        if scope.targets and not any(
            scope_entry_matches_host(item, host) for item in scope.targets
        ):
            return False
        return True

    @staticmethod
    def _fact_evidence_ids(assessment: GoalAssessment | None) -> list[str]:
        if assessment is None:
            return []
        return list(
            dict.fromkeys(
                item_id
                for ref in assessment.evidence_refs
                for kind, separator, item_id in [ref.partition(":")]
                if separator and kind == "fact" and item_id
            )
        )
