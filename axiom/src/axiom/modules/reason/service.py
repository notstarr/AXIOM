from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
from axiom.modules.plan.repository import PlanRepository
from axiom.modules.plan.tables import plan_run_sources, plan_runs
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
MAX_CONSECUTIVE_PLAN_RUNS = 3
REASON_INPUT_KEYS = (
    "source_fact_ids",
    "failed_intent_ids",
    "actionable_todo_ids",
    "todo_review_ids",
    "open_explore_ids",
    "unassessed_intent_ids",
)


def _extract_plan_confidence(reason: str) -> str:
    match = re.search(
        r"(?:confidence|置信度)\s*[:：]\s*([^;；,，\s]+)",
        reason or "",
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else "unspecified"


class ReasonService:
    def __init__(self, engine: Engine, *, store: FileStore | None = None):
        self.engine = engine
        self.store = store
        self.repository = ReasonRepository()
        self.plans = PlanRepository()
        self.tasks = TaskRepository()
        self.todos = TodoRepository()
        self.goals = GoalRepository()
        self.audit = AuditService(engine)

    @staticmethod
    def _unique(values: list[str] | tuple[str, ...] | None) -> list[str]:
        return list(dict.fromkeys(value for value in (values or []) if isinstance(value, str)))

    @classmethod
    def _snapshot_from_state(
        cls,
        state: dict[str, object],
        *,
        source_fact_ids: list[str] | None = None,
    ) -> dict[str, list[str]]:
        return {
            "source_fact_ids": cls._unique(
                source_fact_ids
                if source_fact_ids is not None
                else [row["id"] for row in state["unplanned_facts"]]
            ),
            "failed_intent_ids": cls._unique(list(state["failed_intent_ids"])),
            "actionable_todo_ids": cls._unique(list(state["actionable_todo_ids"])),
            "todo_review_ids": cls._unique(list(state["todo_review_ids"])),
            "open_explore_ids": cls._unique(list(state["open_explore_ids"])),
            "unassessed_intent_ids": cls._unique(list(state["unassessed_intent_ids"])),
        }

    @classmethod
    def _snapshot_from_run(cls, run: ReasonRun) -> dict[str, list[str]]:
        payload = run.trigger_payload or {}
        return {
            "source_fact_ids": cls._unique(
                [*run.source_fact_ids, *payload.get("source_fact_ids", [])]
            ),
            **{
                key: cls._unique(payload.get(key, []))
                for key in REASON_INPUT_KEYS
                if key != "source_fact_ids"
            },
        }

    @classmethod
    def _merge_snapshots(
        cls,
        *snapshots: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        return {
            key: cls._unique(
                [value for snapshot in snapshots for value in snapshot.get(key, [])]
            )
            for key in REASON_INPUT_KEYS
        }

    @staticmethod
    def _snapshot_fingerprint(snapshot: dict[str, list[str]]) -> str:
        return ReasonService._fingerprint(
            source_fact_ids=snapshot["source_fact_ids"],
            failed_intent_ids=snapshot["failed_intent_ids"],
            actionable_todo_ids=snapshot["actionable_todo_ids"],
            todo_review_ids=snapshot["todo_review_ids"],
            open_explore_ids=snapshot["open_explore_ids"],
            unassessed_intent_ids=snapshot["unassessed_intent_ids"],
        )

    @staticmethod
    def _trigger_fingerprint(
        snapshot: dict[str, list[str]],
        *,
        trigger: str,
        trigger_payload: dict,
    ) -> str:
        base = ReasonService._snapshot_fingerprint(snapshot)
        identity: dict[str, object] = {"base": base, "trigger": trigger}
        if trigger == "hint":
            identity["hint_id"] = trigger_payload.get("hint_id")
        elif trigger == "continuation":
            identity["goal_cycle_id"] = trigger_payload.get("goal_cycle_id")
        else:
            return base
        return hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _payload_for_snapshot(
        cls,
        snapshot: dict[str, list[str]],
        *,
        trigger_payload: dict | None = None,
    ) -> dict:
        payload = dict(trigger_payload or {})
        payload.update({key: list(snapshot[key]) for key in REASON_INPUT_KEYS})
        payload["unplanned_fact_count"] = len(snapshot["source_fact_ids"])
        payload["open_explore_count"] = len(snapshot["open_explore_ids"])
        return payload

    @classmethod
    def _has_uncovered_input(
        cls,
        state_snapshot: dict[str, list[str]],
        runs: list[ReasonRun],
    ) -> bool:
        covered = cls._merge_snapshots(*(cls._snapshot_from_run(run) for run in runs))
        return any(
            value not in covered[key]
            for key in REASON_INPUT_KEYS
            for value in state_snapshot[key]
        )

    @staticmethod
    def _queue_limit(config) -> int:
        return max(1, int(getattr(config, "reason_queue_limit", 3)))

    def _enqueue_or_coalesce_in(
        self,
        conn,
        *,
        task,
        trigger: str,
        trigger_payload: dict,
        snapshot: dict[str, list[str]],
        input_fingerprint: str,
        now: str,
    ) -> ReasonRun:
        if conn.execute(
            select(tasks.c.id).where(tasks.c.id == task.id).with_for_update()
        ).scalar_one_or_none() is None:
            raise NotFoundError(f"Task not found: {task.id}")
        pending = [
            run
            for run in self.repository.pending_runs_for_task(conn, task.id)
            if run.trigger != "plan"
        ]
        matched = next(
            (run for run in pending if run.input_fingerprint == input_fingerprint),
            None,
        )
        if matched is not None:
            merged_snapshot = self._merge_snapshots(
                self._snapshot_from_run(matched), snapshot
            )
            merged_payload = self._payload_for_snapshot(
                merged_snapshot,
                trigger_payload=self._merge_trigger_payload(
                    matched.trigger_payload,
                    self._payload_for_snapshot(snapshot, trigger_payload=trigger_payload),
                    trigger=trigger,
                    existing_trigger=matched.trigger,
                ),
            )
            if not self.repository.update_pending_snapshot(
                conn,
                task_id=task.id,
                run_id=matched.id,
                source_fact_ids=merged_snapshot["source_fact_ids"],
                trigger_payload=merged_payload,
                input_fingerprint=matched.input_fingerprint,
                now=now,
            ):
                raise ConflictError("Pending ReasonRun changed while coalescing a trigger")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task.id,
                    reason_run_id=matched.id,
                    event_type="reason_run.coalesced",
                    message="相同输入的 Reason 触发已合并到排队项",
                    payload={
                        "phase": "reason",
                        "trigger": trigger,
                        "input_fingerprint": input_fingerprint,
                        "coalesced_count": merged_payload.get("coalesced_count", 0),
                    },
                ),
            )
            refreshed = self.repository.get_run(conn, task.id, matched.id)
            if refreshed is None:
                raise ConflictError("Coalesced pending ReasonRun disappeared")
            return refreshed

        config = load_runtime_config(conn)
        payload = self._payload_for_snapshot(snapshot, trigger_payload=trigger_payload)
        if len(pending) < self._queue_limit(config):
            return self._create_run(
                conn,
                task=task,
                source_fact_ids=snapshot["source_fact_ids"],
                trigger=trigger,
                trigger_payload=payload,
                input_fingerprint=input_fingerprint,
                now=now,
            )

        tail = pending[-1]
        merged_snapshot = self._merge_snapshots(
            self._snapshot_from_run(tail), snapshot
        )
        merged_payload = self._payload_for_snapshot(
            merged_snapshot,
            trigger_payload=self._merge_trigger_payload(
                tail.trigger_payload,
                payload,
                trigger=trigger,
                existing_trigger=tail.trigger,
            ),
        )
        merged_fingerprint = self._snapshot_fingerprint(merged_snapshot)
        if not self.repository.update_pending_snapshot(
            conn,
            task_id=task.id,
            run_id=tail.id,
            source_fact_ids=merged_snapshot["source_fact_ids"],
            trigger_payload=merged_payload,
            input_fingerprint=merged_fingerprint,
            now=now,
        ):
            raise ConflictError("Pending ReasonRun changed while coalescing a trigger")
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task.id,
                reason_run_id=tail.id,
                event_type="reason_run.coalesced",
                message="新的 Reason 触发已合并到队尾排队项",
                payload={
                    "phase": "reason",
                    "trigger": trigger,
                    "queue_limit": self._queue_limit(config),
                    "coalesced_count": merged_payload.get("coalesced_count", 0),
                },
            ),
        )
        refreshed = self.repository.get_run(conn, task.id, tail.id)
        if refreshed is None:
            raise ConflictError("Merged pending ReasonRun disappeared")
        return refreshed

    @staticmethod
    def _merge_trigger_payload(
        existing: dict,
        incoming: dict,
        *,
        trigger: str,
        existing_trigger: str | None = None,
    ) -> dict:
        merged = dict(existing or {})
        trigger_sources = list(merged.get("trigger_sources") or [])
        original_trigger = (
            merged.get("original_trigger")
            or merged.get("trigger")
            or existing_trigger
        )
        if original_trigger and original_trigger not in trigger_sources:
            trigger_sources.append(original_trigger)
        if trigger not in trigger_sources:
            trigger_sources.append(trigger)
        merged["original_trigger"] = original_trigger or trigger
        merged["last_trigger"] = trigger
        merged["trigger_sources"] = trigger_sources
        merged["coalesced_count"] = int(merged.get("coalesced_count") or 0) + 1
        events = list(merged.get("coalesced_triggers") or [])
        if not events and original_trigger:
            events.append(
                {
                    "trigger": original_trigger,
                    "payload": {
                        key: value
                        for key, value in merged.items()
                        if key not in {*REASON_INPUT_KEYS, "coalesced_triggers"}
                    },
                }
            )
        events.append(
            {
                "trigger": trigger,
                "payload": {
                    key: value
                    for key, value in incoming.items()
                    if key not in REASON_INPUT_KEYS
                },
            }
        )
        merged["coalesced_triggers"] = events
        return merged

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
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if not task.is_runnable_at(now):
            return None
        snapshot = {
            "source_fact_ids": [source_fact_id],
            "failed_intent_ids": [],
            "actionable_todo_ids": [],
            "todo_review_ids": [],
            "open_explore_ids": [],
            "unassessed_intent_ids": [],
        }
        run = self._enqueue_or_coalesce_in(
            conn,
            task=task,
            trigger="bootstrap",
            trigger_payload={"source_fact_id": source_fact_id},
            snapshot=snapshot,
            input_fingerprint=self._snapshot_fingerprint(snapshot),
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
            snapshot = self._snapshot_from_state(state)
            run = self._enqueue_or_coalesce_in(
                conn,
                task=task,
                trigger="manual",
                trigger_payload={
                    "actor": actor,
                    "requested_at": now,
                    "task_phase": task.phase,
                    "unplanned_fact_count": len(source_fact_ids),
                },
                snapshot=snapshot,
                input_fingerprint=self._snapshot_fingerprint(snapshot),
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
        snapshot = self._snapshot_from_state(state)
        trigger_payload = {
            "hint_id": hint.id,
            "hint_content": hint.content,
            "creator": hint.creator,
            "requested_at": now,
            "task_phase": task.phase,
            "unplanned_fact_count": len(source_fact_ids),
        }
        run = self._enqueue_or_coalesce_in(
            conn,
            task=task,
            trigger="hint",
            trigger_payload=trigger_payload,
            snapshot=snapshot,
            input_fingerprint=self._trigger_fingerprint(
                snapshot,
                trigger="hint",
                trigger_payload=trigger_payload,
            ),
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
        snapshot = self._snapshot_from_state(state)
        trigger_payload = {
            "actor": actor,
            "requested_at": now,
            "previous_goal": previous_goal,
            "current_goal": task.goal,
            "continuation_reason": continuation_reason,
            "focus": focus,
            "goal_cycle_id": task.current_goal_cycle_id,
            "goal_achieved_cancelled_intent_ids": cancelled_intent_ids,
        }
        return self._enqueue_or_coalesce_in(
            conn,
            task=task,
            trigger="continuation",
            trigger_payload=trigger_payload,
            snapshot=snapshot,
            input_fingerprint=self._trigger_fingerprint(
                snapshot,
                trigger="continuation",
                trigger_payload=trigger_payload,
            ),
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
        pending = self.repository.pending_runs_for_task(conn, task_id)
        regular_pending = [run for run in pending if run.trigger != "plan"]
        pending_plans = [run for run in pending if run.trigger == "plan"]

        # Plan is an independent runtime.  Reason reconciliation still needs
        # to observe its ownership so a runner tick cannot overwrite the
        # visible phase while the Plan lease is active, or route an idle Task
        # to waiting while a Plan is queued for the next planning pass.  Do
        # not claim or mutate PlanRun rows here; PlanService owns that
        # lifecycle.  This is only a task-lock-protected phase handoff guard.
        independent_plan_active = conn.execute(
            select(plan_runs.c.id)
            .where(
                plan_runs.c.task_id == task_id,
                plan_runs.c.status.in_(("claimed", "running", "concluding")),
            )
            .order_by(plan_runs.c.created_at, plan_runs.c.id)
            .limit(1)
        ).scalar_one_or_none()
        independent_plan_pending = conn.execute(
            select(plan_runs.c.id)
            .where(
                plan_runs.c.task_id == task_id,
                plan_runs.c.status == "pending",
            )
            .order_by(plan_runs.c.created_at, plan_runs.c.id)
            .limit(1)
        ).scalar_one_or_none()
        independent_plan_present = bool(
            independent_plan_active or independent_plan_pending
        )

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
            current = in_flight or (pending[0] if pending else None)
            return current.id if current is not None else None

        state = self._input_state(conn, task_id)
        source_fact_ids = [row["id"] for row in state["unplanned_facts"]]
        open_explore_ids = list(state["open_explore_ids"])
        failed_intent_ids = list(state["failed_intent_ids"])
        actionable_todo_ids = list(state["actionable_todo_ids"])
        todo_review_ids = list(state["todo_review_ids"])
        unassessed_intent_ids = list(state["unassessed_intent_ids"])
        snapshot = self._snapshot_from_state(state)
        signals_present = bool(
            source_fact_ids
            or failed_intent_ids
            or actionable_todo_ids
            or todo_review_ids
            or unassessed_intent_ids
        )

        # An independent Plan can explicitly hand the task back to Reason
        # even when its snapshot contains no newly-unconsumed Fact (the
        # ``needs_reason`` decision is itself a durable planning signal).
        # Do not rely on ``task.phase == 'reason'`` alone: a normal debounce
        # would otherwise send the task straight to waiting, and the handoff
        # would be lost.  The completed PlanRun timestamp makes this a
        # one-shot edge; once the handoff ReasonRun is completed, the latest
        # Reason timestamp is newer and this branch no longer fires.
        latest = self.repository.latest_completed_row(conn, task_id)
        latest_finalized = self.repository.latest_finalized_row(conn, task_id)
        latest_plan = conn.execute(
            select(
                plan_runs.c.id,
                plan_runs.c.concluded_at,
                plan_runs.c.updated_at,
                plan_runs.c.decision_json,
            )
            .where(
                plan_runs.c.task_id == task_id,
                plan_runs.c.status == "completed",
            )
            .order_by(
                plan_runs.c.concluded_at.desc().nulls_last(),
                plan_runs.c.id.desc(),
            )
            .limit(1)
        ).mappings().one_or_none()
        plan_handoff_pending = False
        if task.phase == "reason" and latest_plan is not None:
            plan_finished_at = latest_plan.get("concluded_at") or latest_plan.get(
                "updated_at"
            )
            # Include failed/cancelled terminal ReasonRuns here.  A Plan
            # handoff is consumed even when its Reason attempt fails; using
            # only the latest completed row would recreate the same handoff
            # forever and bypass the normal retry budget.
            reason_finished_at = (
                latest_finalized.get("concluded_at")
                if latest_finalized is not None
                else None
            )
            try:
                plan_handoff_pending = reason_finished_at is None or (
                    plan_finished_at is not None
                    and parse_utc(str(plan_finished_at)) > parse_utc(str(reason_finished_at))
                )
            except (TypeError, ValueError):
                # A malformed historical timestamp must not prevent normal
                # reconciliation; the explicit phase still provides a safe
                # fallback for a subsequent manual trigger.
                plan_handoff_pending = False

        if plan_handoff_pending and not in_flight and not pending:
            try:
                plan_decision = json.loads(latest_plan.get("decision_json") or "{}")
            except (TypeError, ValueError):
                plan_decision = {}
            handoff_payload = {
                "plan_handoff": True,
                "plan_run_id": latest_plan["id"],
                "plan_decision": plan_decision.get("decision", "needs_reason"),
                "handoff_reason": "independent_plan_completed",
                "source_fact_ids": list(snapshot["source_fact_ids"]),
                "failed_intent_ids": list(snapshot["failed_intent_ids"]),
                "actionable_todo_ids": list(snapshot["actionable_todo_ids"]),
                "todo_review_ids": list(snapshot["todo_review_ids"]),
                "open_explore_ids": list(snapshot["open_explore_ids"]),
                "unassessed_intent_ids": list(snapshot["unassessed_intent_ids"]),
            }
            handoff = self._enqueue_or_coalesce_in(
                conn,
                task=task,
                trigger="plan_handoff",
                trigger_payload=handoff_payload,
                snapshot=snapshot,
                input_fingerprint=self._snapshot_fingerprint(snapshot),
                now=now,
            )
            self._set_phase(conn, task_id, "reason", now)
            return handoff.id

        # A pending Plan is conditional on the incremental queue being empty.
        # If new input arrives before it is claimed, cancel only that pending
        # Plan and let the ordinary Reason FIFO handle the signal.  An already
        # claimed/running Plan remains valid and late-binds the new input at
        # start/commit time.
        if (
            (pending_plans or independent_plan_pending)
            and in_flight is None
            and (signals_present or open_explore_ids)
        ):
            if pending_plans:
                self.repository.cancel_pending_plan_runs(
                    conn,
                    task_id,
                    now=now,
                    failure_kind="incremental_work_arrived",
                )
            if independent_plan_pending:
                self.plans.cancel_pending_for_task(
                    conn,
                    task_id,
                    now=now,
                    failure_kind="incremental_work_arrived",
                )
            pending = regular_pending
            pending_plans = []
            independent_plan_pending = None
            independent_plan_present = bool(independent_plan_active)
        open_runs = [run for run in [in_flight, *pending] if run is not None]
        if open_runs and not self._has_uncovered_input(snapshot, open_runs):
            current = in_flight or (regular_pending[0] if regular_pending else pending[0])
            self._set_phase(
                conn,
                task_id,
                "plan"
                if independent_plan_present
                else ("plan" if current.trigger == "plan" else "reason"),
                now,
            )
            return (in_flight or pending[0]).id
        # A terminal Explore without a follow-up is itself a planning signal.
        if not signals_present:
            if open_explore_ids:
                self._set_phase(
                    conn,
                    task_id,
                    "plan" if independent_plan_present else "explore",
                    now,
                )
                return None
            if pending_plans:
                self._set_phase(conn, task_id, "plan", now)
                return pending_plans[0].id
            if independent_plan_present:
                self._set_phase(conn, task_id, "plan", now)
                return None
            # Plan is an independent runtime.  Reason only reports that its
            # incremental work is exhausted; the dispatcher invokes
            # PlanService.reconcile_task() in the same tick.  Keeping Plan
            # creation out of this service is important: a new panorama pass
            # must never be persisted as reason_runs(trigger="plan").
            self._set_phase(conn, task_id, "waiting", now)
            return None

        config = load_runtime_config(conn)
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
        if open_runs:
            if unassessed_intent_ids and not open_explore_ids:
                trigger = "intent_assessment"
            elif source_fact_ids:
                trigger = "fact_batch"
            else:
                trigger = "low_watermark"
        elif latest is None:
            trigger = "bootstrap"
        else:
            last_finished = latest["concluded_at"] or latest["updated_at"]
            elapsed = max(0.0, (parse_utc(now) - parse_utc(last_finished)).total_seconds())
            after_plan_handoff = task.phase == "reason" and latest["trigger"] == "plan"
            # Unassessed terminal Intents are a correctness signal, not a
            # throughput hint.  Do not let the normal Reason debounce leave
            # them waiting (and potentially forgotten) after Explore ends.
            # The same rule applies to input which arrived during Plan: the
            # Plan commit has explicitly handed that state to Reason, so a
            # normal debounce/low-watermark setting must not strand it.
            if (
                elapsed < config.reason_min_interval_seconds
                and not unassessed_intent_ids
                and not after_plan_handoff
            ):
                self._set_phase(
                    conn,
                    task_id,
                    "plan"
                    if independent_plan_present
                    else ("explore" if open_explore_ids else "waiting"),
                    now,
                )
                return None
            if after_plan_handoff and unassessed_intent_ids and not open_explore_ids:
                trigger = "intent_assessment"
            elif after_plan_handoff and source_fact_ids:
                trigger = "fact_batch"
            elif after_plan_handoff and signals_present:
                trigger = "low_watermark"
            elif len(source_fact_ids) >= config.reason_batch_size:
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
                self._set_phase(
                    conn,
                    task_id,
                    "plan"
                    if independent_plan_present
                    else ("explore" if open_explore_ids else "waiting"),
                    now,
                )
                return None

        fingerprint = self._snapshot_fingerprint(snapshot)
        latest_deduplication = self.repository.latest_deduplication_row(conn, task_id)
        if (
            not open_runs
            and
            not unassessed_intent_ids
            and
            latest_deduplication is not None
            and latest_deduplication["input_fingerprint"] == fingerprint
        ):
            self._set_phase(
                conn,
                task_id,
                "plan"
                if independent_plan_present
                else ("explore" if open_explore_ids else "waiting"),
                now,
            )
            return None

        run = self._enqueue_or_coalesce_in(
            conn,
            task=task,
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
            snapshot=snapshot,
            input_fingerprint=fingerprint,
            now=now,
        )
        # If a Plan lease is already active, the newly queued Reason input is
        # deliberately held behind it.  Plan commit will hand the Task back
        # to Reason once the Plan closes; exposing phase=reason here would let
        # the next UI/runner tick misreport the current owner.
        self._set_phase(conn, task_id, "plan" if independent_plan_present else "reason", now)
        return run.id

    def _create_plan_if_allowed_in(self, conn, *, task, state, now: str) -> ReasonRun | None:
        """Deprecated compatibility hook; PlanService owns Plan creation.

        This method intentionally never writes a ReasonRun.  It remains as a
        small no-op for callers from older integrations while the independent
        Plan runtime is rolled out.
        """
        return None

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
                payload={
                    "phase": "plan" if trigger == "plan" else "reason",
                    "trigger": trigger,
                    **trigger_payload,
                },
            ),
        )
        return run

    def _input_state(self, conn, task_id: str) -> dict[str, object]:
        planned_by_reason = exists(
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
        # Plan owns the same Fact queue through a separate table.  A Fact
        # captured by a pending/active/completed PlanRun must not be offered
        # again to Reason after Plan concludes; otherwise an exhausted Plan
        # would immediately create a duplicate Reason pass for its baseline
        # inputs.  Failed/cancelled PlanRuns intentionally release ownership
        # so reconciliation can retry the input.
        planned_by_plan = exists(
            select(plan_run_sources.c.fact_id)
            .join(
                plan_runs,
                (plan_runs.c.task_id == plan_run_sources.c.task_id)
                & (plan_runs.c.id == plan_run_sources.c.plan_run_id),
            )
            .where(
                plan_run_sources.c.task_id == task_id,
                plan_run_sources.c.fact_id == facts.c.id,
                plan_runs.c.status.in_(
                    ["pending", "claimed", "running", "concluding", "completed"]
                ),
            )
        )
        planned_fact_exists = or_(planned_by_reason, planned_by_plan)
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
        failed_conditions = [
            intents.c.task_id == task_id,
            intents.c.kind == "explore",
            intents.c.status.in_(["failed", "cancelled"]),
            ~(
                (intents.c.status == "cancelled")
                & (intents.c.last_failure_kind == "goal_achieved")
            ),
            intents.c.archived.is_(False),
        ]
        assessment_status = intents.c.get("assessment_status")
        if assessment_status is not None:
            # A terminal failure is an incremental Reason signal exactly until
            # its one-time assessment is committed.  Keeping every historical
            # failure here forever prevents the queue from ever becoming empty
            # and therefore prevents Plan from running at all.
            failed_conditions.append(assessment_status == "open")
        failed_intent_ids = list(
            conn.execute(
                select(intents.c.id)
                .where(*failed_conditions)
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
            if conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            ).scalar_one_or_none() is None:
                raise NotFoundError(f"Task not found: {task_id}")
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
            state = self._snapshot_from_state(self._input_state(conn, task_id))
            other_pending = [
                item
                for item in self.repository.pending_runs_for_task(conn, task_id)
                if item.id != reason_run_id
            ]
            reserved = self._merge_snapshots(
                *(self._snapshot_from_run(item) for item in other_pending)
            )
            late_bound = self._merge_snapshots(
                self._snapshot_from_run(run),
                {
                    key: [value for value in state[key] if value not in reserved[key]]
                    for key in REASON_INPUT_KEYS
                },
            )
            payload = self._payload_for_snapshot(
                late_bound,
                trigger_payload={
                    **run.trigger_payload,
                    "late_bound": True,
                    "late_bound_at": now,
                },
            )
            input_fingerprint = (
                self._trigger_fingerprint(
                    late_bound,
                    trigger=run.trigger,
                    trigger_payload=payload,
                )
                if run.trigger in {"hint", "continuation"}
                else self._snapshot_fingerprint(late_bound)
            )
            if not self.repository.update_run_snapshot(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                source_fact_ids=late_bound["source_fact_ids"],
                trigger_payload=payload,
                input_fingerprint=input_fingerprint,
                now=now,
            ):
                raise ConflictError("ReasonRun snapshot refresh lost a concurrent race")
            if not self.repository.start(
                conn,
                task_id=task_id,
                run_id=reason_run_id,
                lease_id=lease_id,
                now=now,
            ):
                raise ConflictError("ReasonRun start lost a concurrent race")
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    reason_run_id=reason_run_id,
                    worker=run.worker or "",
                    event_type="reason_run.started",
                    message="ReasonRun 开始执行",
                    payload={
                        "phase": "plan" if run.trigger == "plan" else "reason",
                        "lease_id": lease_id,
                    },
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
            # Legacy panorama rows are retained for history only.  Do not let
            # a stale lease revive them through the Reason heartbeat path.
            self._require_run_lease(conn, task_id, reason_run_id, lease_id)
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
                        "phase": "plan" if run.trigger == "plan" else "reason",
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
        if run.trigger == "plan":
            raise ConflictError(
                "Legacy ReasonRun(trigger=plan) is read-only; use the independent PlanRun API"
            )
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
                    context_revisions.c.phase == (
                        "plan" if run.trigger == "plan" else "reason"
                    ),
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
                    payload={
                        "phase": "plan" if run.trigger == "plan" else "reason",
                        "workspace_id": workspace.id,
                    },
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
                        "phase": (
                            "plan"
                            if self._workspace_run(conn, workspace).trigger == "plan"
                            else "reason"
                        ),
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
            if run.trigger == "plan":
                proposed_fingerprint = self._intent_fingerprint(
                    description=proposal.description,
                    targets=proposal.targets,
                )
                if any(
                    self._intent_fingerprint(
                        description=item.description,
                        targets=item.targets,
                    )
                    == proposed_fingerprint
                    for item in workspace.draft.intent_proposals
                ):
                    raise ConflictError(
                        "Plan Workspace 中已经存在相同 target/endpoint/test_vector 的 Intent"
                    )
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
            run = self._workspace_run(conn, workspace)
            self._reject_plan_goal_completion(run, assessment)
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
                self._reject_plan_goal_completion(run, assessment)
                task = self.tasks.get(conn, workspace.task_id)
                assert task is not None
                now = utcnow()
                self._align_todo_counter(conn, workspace.task_id)
                todo_refs: dict[str, str] = {}
                created_todo_ids: list[str] = []
                updated_todo_ids: list[str] = []
                deleted_todo_ids: list[str] = []

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
                    updated = self.todos.update(
                        conn,
                        workspace.task_id,
                        update_command.todo_id,
                        values,
                    )
                    if updated is not None:
                        updated_todo_ids.append(update_command.todo_id)
                    self.todos.add_sources(
                        conn,
                        task_id=workspace.task_id,
                        todo_id=update_command.todo_id,
                        sources=update_command.source_adds,
                    )

                # 执行已持久化 Todo 的归档/软删除
                # 注意：todos.status 仅允许 open/partial/blocked/completed/
                # rejected/obsolete（DB 约束 ck_todos_status），"cancelled"
                # 是 intents/reason_runs 的状态，不能用于 todos（会导致
                # commit 500 → abandon → control_plane_unavailable 死循环）。
                for delete_command in draft.todo_deletions:
                    if delete_command.todo_id in todo_refs:
                        continue
                    deleted = self.todos.update(
                        conn,
                        workspace.task_id,
                        delete_command.todo_id,
                        {
                            "archived": True,
                            "status": "obsolete",
                            "status_reason": delete_command.reason,
                            "updated_at": now,
                        },
                    )
                    if deleted is not None:
                        deleted_todo_ids.append(delete_command.todo_id)

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

                plan_decision: str | None = None
                plan_post_signals: dict[str, object] | None = None
                if run.trigger == "plan":
                    if created_intent_ids:
                        plan_decision = "explore"
                    else:
                        plan_post_signals = self._input_state(conn, workspace.task_id)
                        has_post_plan_signals = any(
                            plan_post_signals[key]
                            for key in (
                                "unplanned_facts",
                                "failed_intent_ids",
                                "actionable_todo_ids",
                                "todo_review_ids",
                                "unassessed_intent_ids",
                            )
                        )
                        plan_decision = "reason" if has_post_plan_signals else "waiting"
                    plan_payload = {
                        **run.trigger_payload,
                        "decision": plan_decision,
                        "reviewed_dimensions": run.trigger_payload.get(
                            "reviewed_dimensions",
                            [
                                "coverage",
                                "weaponization",
                                "blocked_paths",
                                "goal_completion",
                            ],
                        ),
                        "evidence_refs": list(assessment.evidence_refs),
                        "exhaustion_reason": (
                            assessment.reason if plan_decision == "waiting" else ""
                        ),
                        "confidence": _extract_plan_confidence(assessment.reason),
                    }
                    if plan_post_signals is not None:
                        plan_payload["post_plan_input_counts"] = {
                            "facts": len(plan_post_signals["unplanned_facts"]),
                            "failed_intents": len(plan_post_signals["failed_intent_ids"]),
                            "actionable_todos": len(plan_post_signals["actionable_todo_ids"]),
                            "todo_reviews": len(plan_post_signals["todo_review_ids"]),
                            "unassessed_intents": len(plan_post_signals["unassessed_intent_ids"]),
                        }
                    if not self.repository.update_trigger_payload(
                        conn,
                        task_id=workspace.task_id,
                        run_id=run.id,
                        trigger_payload=plan_payload,
                        now=now,
                    ):
                        raise ConflictError("Plan metadata update lost a concurrent race")

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
                    if open_count:
                        next_phase = "explore"
                    elif run.trigger == "plan":
                        # Inputs arriving while Plan was running must get a
                        # normal Reason pass before the Task can wait. Plan's
                        # own snapshot is already marked consumed; only new
                        # state remains visible here.
                        post_plan_state = plan_post_signals or self._input_state(
                            conn, workspace.task_id
                        )
                        post_plan_signals = any(
                            post_plan_state[key]
                            for key in (
                                "unplanned_facts",
                                "failed_intent_ids",
                                "actionable_todo_ids",
                                "todo_review_ids",
                                "unassessed_intent_ids",
                            )
                        )
                        next_phase = "reason" if post_plan_signals else "waiting"
                    else:
                        next_phase = "waiting"
                    conn.execute(
                        update(tasks)
                        .where(tasks.c.id == workspace.task_id)
                        .values(phase=next_phase, updated_at=now)
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
                            "phase": "plan" if run.trigger == "plan" else "reason",
                            "created_todo_ids": created_todo_ids,
                            "updated_todo_ids": updated_todo_ids,
                            "deleted_todo_ids": deleted_todo_ids,
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
        if run.trigger == "plan":
            raise ConflictError(
                "Legacy Reason Workspace for Plan is read-only; use Plan Workspace"
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
        if run.trigger == "plan":
            raise ConflictError(
                "Legacy Reason Workspace for Plan is read-only; use Plan Workspace"
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
        assessment = draft.goal_assessment
        unconsumed_fact_ids = self._unconsumed_source_fact_ids(workspace, run)
        # An achieved Goal is a Reason decision.  Its source Facts remain
        # available as context, but they do not need to be attached as
        # ``evidence_refs`` (or consumed through a Fact -> Goal relation).
        if unconsumed_fact_ids and not (assessment and assessment.achieved):
            errors.append(
                "本轮新 Fact 尚未纳入规划或 Goal 评估："
                + ", ".join(unconsumed_fact_ids)
            )
        if assessment is None:
            errors.append("缺少 Goal assessment")
        else:
            try:
                self._validate_goal_assessment(conn, workspace, assessment)
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
            if assessment.achieved and draft.intent_proposals:
                errors.append("Goal 已达成时不能继续派发 Intent")
            if run.trigger == "plan" and assessment.achieved:
                errors.append("Plan 阶段不能直接将 Goal 标记为已达成")
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
        if assessment is not None and not assessment.achieved:
            # Keep the legacy acknowledgement path for an unachieved run:
            # a supplied Fact reference tells Reason that the input was
            # reviewed.  This is not used to prove completion; achieved runs
            # are allowed to finish without any Fact reference.
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

    @staticmethod
    def _reject_plan_goal_completion(run: ReasonRun, assessment: GoalAssessment) -> None:
        if run.trigger == "plan" and assessment.achieved:
            raise ConflictError(
                "Plan 只能进行全景复盘和重新规划，不能直接将 Goal 标记为 achieved；"
                "请使用 achieved=false 并提交可审计的复盘结论"
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
    def _align_todo_counter(conn, task_id: str) -> None:
        """Bring the task todo counter at least past the highest existing id."""
        rows = conn.execute(
            select(todos.c.id).where(todos.c.task_id == task_id)
        ).scalars().all()
        max_num = 0
        for rid in rows:
            if rid.startswith("td") and rid[2:].isdigit():
                max_num = max(max_num, int(rid[2:]))
        conn.execute(
            update(id_counters)
            .where(
                id_counters.c.scope == task_id,
                id_counters.c.kind == "todo",
            )
            .values(next_value=func.greatest(id_counters.c.next_value, max_num + 1))
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
        if run.trigger == "plan":
            self._reject_repeated_plan_intent(
                conn,
                task_id=task_id,
                proposal=proposal,
            )

    def _reject_repeated_plan_intent(self, conn, *, task_id: str, proposal: DraftIntent) -> None:
        """Block a Plan from replaying a failed vector without new evidence."""

        proposed_fingerprint = self._intent_fingerprint(
            description=proposal.description,
            targets=proposal.targets,
        )
        rows = conn.execute(
            select(
                intents.c.id,
                intents.c.status,
                intents.c.description,
                intents.c.targets_json,
                intents.c.concluded_at,
            ).where(
                intents.c.task_id == task_id,
                intents.c.kind == "explore",
            )
        ).mappings()
        for row in rows:
            try:
                targets = json.loads(row["targets_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                targets = []
            if self._intent_fingerprint(
                description=row["description"] or "",
                targets=targets,
            ) != proposed_fingerprint:
                continue
            if row["status"] in OPEN_EXPLORE_STATUSES:
                raise ConflictError(
                    f"Plan Intent 与正在执行的历史 Intent 重复：{row['id']}"
                )
            if row["status"] in TERMINAL_EXPLORE_STATUSES:
                new_fact = None
                if row["concluded_at"] and proposal.from_facts:
                    new_fact = conn.execute(
                        select(facts.c.id)
                        .where(
                            facts.c.task_id == task_id,
                            facts.c.id.in_(proposal.from_facts),
                            facts.c.archived.is_(False),
                            facts.c.created_at > row["concluded_at"],
                        )
                        .limit(1)
                    ).scalar_one_or_none()
                if new_fact is None:
                    raise ConflictError(
                        f"Plan Intent 与历史失败/完成向量重复且没有新 Fact：{row['id']}"
                    )

    @staticmethod
    def _intent_identity(proposal: DraftIntent) -> tuple:
        return (
            proposal.todo_ref or proposal.todo_id,
            proposal.description,
            tuple(proposal.targets),
        )

    @staticmethod
    def _intent_fingerprint(*, description: str, targets: list[str]) -> str:
        markers: dict[str, str] = {}
        for part in re.split(r"[;；\n]", description or ""):
            key, separator, value = part.partition(":")
            if separator and key.strip().casefold() in {
                "endpoint",
                "test_vector",
                "test vector",
                "target",
            }:
                normalized_key = key.strip().casefold().replace(" ", "_")
                markers[normalized_key] = " ".join(value.split()).casefold()
        normalized_description = " ".join((description or "").split()).casefold()
        payload = {
            "target": sorted(
                {" ".join((target or "").split()).casefold() for target in targets or []}
            ),
            "endpoint": markers.get("endpoint") or normalized_description,
            "test_vector": markers.get("test_vector") or normalized_description,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

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
