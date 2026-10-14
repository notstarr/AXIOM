from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, exists, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.dispatch.pool import json_list, narrow_pool_after_failure
from axiom.modules.dispatch.retry import retry_blocked_message, retry_outcome
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.context.tables import context_revisions
from axiom.modules.execution.intent_budget import resolve_explore_budget
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.plan.repository import LEASED_PLAN_STATUSES, PlanRepository
from axiom.modules.plan.tables import plan_run_sources, plan_runs, plan_workspaces
from axiom.modules.plan.schemas import (
    DraftChain,
    DraftChainIntentLink,
    DraftChainUpdate,
    DraftPlanIntent,
    DraftPlanTodo,
    DraftPlanTodoDelete,
    DraftPlanTodoUpdate,
    PlanCommitResult,
    PlanDecision,
    PlanReconcileReport,
    PlanRun,
    PlanWorkspace,
    PlanWorkspaceStatus,
    ReleasedPlanRun,
    StartedPlanRun,
)
from axiom.modules.reason.tables import reason_runs, reason_run_sources
from axiom.modules.redteam.schemas import RedTeamChainCreate
from axiom.modules.settings.service import load_runtime_config
from axiom.modules.settings.tables import runtime_settings
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.repository import TodoRepository
from axiom.modules.todos.schemas import (
    CreateTodo,
    TODO_STATUS_REASON_REQUIRED,
    TodoSource,
)
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerability_artifacts
from axiom.modules.tasks.origin import parse_intent_network_target, scope_entry_matches_host
from axiom.shared.clock import utcnow
from axiom.shared.database import run_transaction, transaction
from axiom.shared.errors import ConflictError, LeaseInactiveError, NotFoundError
from axiom.shared.ids import allocate_id, id_counters


if TYPE_CHECKING:
    from axiom.modules.sandboxes.service import TaskSandboxManager


logger = logging.getLogger(__name__)


MAX_PLAN_INTENTS = 256
OPEN_EXPLORE_STATUSES = ("pending", "claimed", "running", "concluding")
TERMINAL_EXPLORE_STATUSES = ("completed", "failed", "cancelled")
PLAN_SNAPSHOT_KEYS = (
    "source_fact_ids",
    "failed_intent_ids",
    "actionable_todo_ids",
    "todo_review_ids",
    "open_explore_ids",
    "unassessed_intent_ids",
)
PLAN_SOURCE_REFERENCE_KINDS = frozenset(
    {
        "fact",
        "intent",
        "todo",
        "artifact",
        "finding",
        "lead",
        "evidence",
        "hint",
        "knowledge",
        "goal_evaluation",
    }
)


class PlanService:
    """Independent Plan domain service.

    This service intentionally does not import ReasonService, ReasonRunner or
    Reason schemas.  It owns PlanRun/PlanWorkspace persistence and only calls
    shared Todo/Intent repositories when committing a valid Plan draft.
    """

    def __init__(self, engine: Engine, *, sandboxes: TaskSandboxManager | None = None, red_team=None):
        self.engine = engine
        self.repository = PlanRepository()
        self.tasks = TaskRepository()
        self.sandboxes = sandboxes
        self.todos = TodoRepository()
        self.red_team = red_team
        self.audit = AuditService(engine)

    @staticmethod
    def _fingerprint(source_fact_ids: list[str], payload: dict[str, Any]) -> str:
        value = json.dumps(
            {"facts": sorted(set(source_fact_ids)), "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: list[str] | tuple[str, ...] | None) -> list[str]:
        return list(dict.fromkeys(value for value in (values or []) if isinstance(value, str) and value))

    @classmethod
    def _snapshot_from_run(cls, run: PlanRun) -> dict[str, list[str]]:
        payload = run.trigger_payload or {}
        return {
            "source_fact_ids": cls._unique([*run.source_fact_ids, *payload.get("source_fact_ids", [])]),
            **{
                key: cls._unique(payload.get(key, []))
                for key in PLAN_SNAPSHOT_KEYS
                if key != "source_fact_ids"
            },
        }

    @classmethod
    def _merge_snapshots(cls, *snapshots: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            key: cls._unique([value for snapshot in snapshots for value in snapshot.get(key, [])])
            for key in PLAN_SNAPSHOT_KEYS
        }

    @staticmethod
    def _snapshot_fingerprint(snapshot: dict[str, list[str]]) -> str:
        value = json.dumps(
            {key: sorted(set(snapshot.get(key, []))) for key in PLAN_SNAPSHOT_KEYS},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _merge_trigger_payload(
        existing: dict[str, Any] | None,
        incoming: dict[str, Any],
        *,
        trigger: str,
        existing_trigger: str | None = None,
    ) -> dict[str, Any]:
        """Merge trigger provenance without dropping a coalesced Plan signal.

        A repeated snapshot is still a real trigger (for example two manual
        requests with different actors).  Preserve that provenance in the
        pending row instead of returning the queue item silently.
        """
        merged = dict(existing or {})
        sources = list(merged.get("trigger_sources") or [])
        original = merged.get("original_trigger") or existing_trigger
        if original and original not in sources:
            sources.append(original)
        if trigger not in sources:
            sources.append(trigger)
        merged["original_trigger"] = original or trigger
        merged["last_trigger"] = trigger
        merged["trigger_sources"] = sources
        merged["coalesced_count"] = int(merged.get("coalesced_count") or 0) + 1
        events = list(merged.get("coalesced_triggers") or [])
        if not events and original:
            events.append(
                {
                    "trigger": original,
                    "payload": {
                        key: value
                        for key, value in merged.items()
                        if key not in {*PLAN_SNAPSHOT_KEYS, "coalesced_triggers"}
                    },
                }
            )
        events.append(
            {
                "trigger": trigger,
                "payload": {
                    key: value
                    for key, value in incoming.items()
                    if key not in PLAN_SNAPSHOT_KEYS
                },
            }
        )
        merged["coalesced_triggers"] = events
        return merged

    def _audit_in(
        self,
        conn,
        *,
        task,
        event_type: str,
        message: str,
        plan_run_id: str | None = None,
        workspace_id: str | None = None,
        worker: str = "",
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task.id,
                plan_run_id=plan_run_id,
                workspace_id=workspace_id,
                worker=worker,
                event_type=event_type,
                level=level,
                message=message,
                payload=payload or {},
            ),
        )

    @staticmethod
    def _scope_target_allowed(task, target: str) -> bool:
        scope = task.scope
        if not scope.enabled:
            return True
        try:
            candidate = parse_intent_network_target(target)
        except Exception:
            return False
        if candidate is None:
            return True
        if any(scope_entry_matches_host(item, candidate.host) for item in scope.exclude):
            return False
        return not scope.targets or any(
            scope_entry_matches_host(item, candidate.host) for item in scope.targets
        )

    @staticmethod
    def _current_snapshot(conn, task_id: str) -> dict[str, list[str]]:
        """Return the durable planning signals visible to a new Plan pass.

        This intentionally lives in the Plan domain instead of calling the
        Reason service.  A Plan sees the whole historical graph, while these
        signal lists identify inputs that arrived after its creation and still
        need a normal planning consumer.
        """
        # ``source_fact_ids`` is an ownership snapshot, not the complete
        # historical Plan view.  Plan Context already exposes the full graph
        # (including archived history); putting every historical Fact into
        # ``plan_run_sources`` would incorrectly mark old evidence as
        # consumed/reserved by this Plan and would hide it from subsequent
        # incremental planning.  Only Facts that no other planning run has
        # reserved are owned by this run.  The full historical graph remains
        # available through ContextService's phase="plan" view.
        fact_ids = PlanService._unconsumed_fact_ids(conn, task_id)
        assessment_status = intents.c.get("assessment_status")
        failed_conditions = [
            intents.c.task_id == task_id,
            intents.c.kind == "explore",
            intents.c.status.in_(TERMINAL_EXPLORE_STATUSES),
            intents.c.archived.is_(False),
            ~(
                (intents.c.status == "cancelled")
                & (intents.c.last_failure_kind == "goal_achieved")
            ),
        ]
        unassessed_conditions = list(failed_conditions)
        if assessment_status is not None:
            failed_conditions.append(assessment_status == "open")
            unassessed_conditions.append(assessment_status == "open")
        failed_intent_ids = list(
            conn.execute(select(intents.c.id).where(*failed_conditions).order_by(intents.c.created_at, intents.c.id)).scalars()
        )
        unassessed_intent_ids = list(
            conn.execute(select(intents.c.id).where(*unassessed_conditions).order_by(intents.c.created_at, intents.c.id)).scalars()
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
        has_open_explore = exists(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.todo_id == todos.c.id,
                intents.c.kind == "explore",
                intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
            )
        )
        has_terminal_explore = exists(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.todo_id == todos.c.id,
                intents.c.kind == "explore",
                intents.c.status.in_(TERMINAL_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
            )
        )
        actionable_todo_ids = list(
            conn.execute(
                select(todos.c.id)
                .where(
                    todos.c.task_id == task_id,
                    todos.c.status.in_(["open", "partial"]),
                    todos.c.archived.is_(False),
                    ~has_open_explore,
                )
                .order_by(todos.c.priority.desc(), todos.c.id)
            ).scalars()
        )
        todo_review_ids = list(
            conn.execute(
                select(todos.c.id)
                .where(
                    todos.c.task_id == task_id,
                    todos.c.status.in_(["open", "partial"]),
                    todos.c.archived.is_(False),
                    has_terminal_explore,
                    ~has_open_explore,
                )
                .order_by(todos.c.priority.desc(), todos.c.id)
            ).scalars()
        )
        return {
            "source_fact_ids": fact_ids,
            "failed_intent_ids": failed_intent_ids,
            "actionable_todo_ids": actionable_todo_ids,
            "todo_review_ids": todo_review_ids,
            "open_explore_ids": open_explore_ids,
            "unassessed_intent_ids": unassessed_intent_ids,
        }

    def _refresh_snapshot_in(self, conn, *, task_id: str, run: PlanRun, now: str) -> PlanRun:
        current = self._current_snapshot(conn, task_id)
        baseline = self._snapshot_from_run(run)
        reserved = self._reserved_snapshot_ids(conn, task_id, exclude_plan_run_id=run.id)
        late_current = {
            key: [value for value in current[key] if value in baseline[key] or value not in reserved[key]]
            for key in PLAN_SNAPSHOT_KEYS
        }
        merged = self._merge_snapshots(baseline, late_current)
        payload = dict(run.trigger_payload or {})
        payload.update({key: list(merged[key]) for key in PLAN_SNAPSHOT_KEYS})
        payload.update({
            "source_fact_ids": list(merged["source_fact_ids"]),
            "late_bound": True,
            "late_bound_at": now,
        })
        fingerprint = self._snapshot_fingerprint(merged)
        if not self.repository.update_run_snapshot(
            conn,
            task_id=task_id,
            run_id=run.id,
            source_fact_ids=merged["source_fact_ids"],
            trigger_payload=payload,
            input_fingerprint=fingerprint,
            now=now,
        ):
            raise ConflictError("PlanRun input snapshot changed while starting")
        refreshed = self.repository.get_run(conn, task_id, run.id)
        if refreshed is None:
            raise ConflictError("PlanRun disappeared while refreshing its input snapshot")
        return refreshed

    @classmethod
    def _reserved_snapshot_ids(
        cls,
        conn,
        task_id: str,
        *,
        exclude_plan_run_id: str | None = None,
    ) -> dict[str, set[str]]:
        reserved = {key: set() for key in PLAN_SNAPSHOT_KEYS}
        reason_rows = conn.execute(
            select(reason_runs.c.id, reason_runs.c.trigger_payload_json)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(["pending", "claimed", "running", "concluding", "completed"]),
                reason_runs.c.trigger != "plan",
            )
        ).mappings()
        plan_rows = conn.execute(
            select(plan_runs.c.id, plan_runs.c.trigger_payload_json)
            .where(
                plan_runs.c.task_id == task_id,
                plan_runs.c.status.in_(["pending", "claimed", "running", "concluding", "completed"]),
                plan_runs.c.id != (exclude_plan_run_id or ""),
            )
        ).mappings()
        for row in [*reason_rows, *plan_rows]:
            try:
                payload = json.loads(row["trigger_payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            for key in PLAN_SNAPSHOT_KEYS:
                reserved[key].update(
                    value for value in payload.get(key, []) if isinstance(value, str)
                )
        reason_source_rows = conn.execute(
            select(reason_run_sources.c.fact_id)
            .join(
                reason_runs,
                (reason_runs.c.task_id == reason_run_sources.c.task_id)
                & (reason_runs.c.id == reason_run_sources.c.reason_run_id),
            )
            .where(
                reason_run_sources.c.task_id == task_id,
                reason_runs.c.status.in_(["pending", "claimed", "running", "concluding", "completed"]),
                reason_runs.c.trigger != "plan",
            )
        ).scalars()
        reserved["source_fact_ids"].update(reason_source_rows)
        plan_source_query = select(plan_run_sources.c.fact_id).join(
            plan_runs,
            (plan_runs.c.task_id == plan_run_sources.c.task_id)
            & (plan_runs.c.id == plan_run_sources.c.plan_run_id),
        ).where(
            plan_run_sources.c.task_id == task_id,
            plan_runs.c.status.in_(["pending", "claimed", "running", "concluding", "completed"]),
            plan_runs.c.id != (exclude_plan_run_id or ""),
        )
        reserved["source_fact_ids"].update(conn.execute(plan_source_query).scalars())
        return reserved

    @staticmethod
    def _unconsumed_fact_ids(conn, task_id: str) -> list[str]:
        reason_consumed = exists(
            select(reason_run_sources.c.fact_id)
            .join(
                reason_runs,
                (reason_runs.c.task_id == reason_run_sources.c.task_id)
                & (reason_runs.c.id == reason_run_sources.c.reason_run_id),
            )
            .where(
                reason_run_sources.c.task_id == task_id,
                reason_run_sources.c.fact_id == facts.c.id,
                reason_runs.c.status.in_(
                    ["pending", "claimed", "running", "concluding", "completed"]
                ),
                reason_runs.c.trigger != "plan",
            )
        )
        plan_consumed = exists(
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
        return list(
            conn.execute(
                select(facts.c.id)
                .where(
                    facts.c.task_id == task_id,
                    facts.c.archived.is_(False),
                    facts.c.id.not_in(["origin", "goal"]),
                    ~reason_consumed,
                    ~plan_consumed,
                )
                .order_by(facts.c.created_at, facts.c.id)
            ).scalars()
        )

    def _planning_blocked(self, conn, task_id: str) -> bool:
        """Whether incremental work still has a higher-priority owner."""
        bootstrap_done = conn.execute(
            select(intents.c.id)
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "bootstrap",
                intents.c.status == "completed",
                intents.c.archived.is_(False),
            )
            .limit(1)
        ).scalar_one_or_none()
        if bootstrap_done is None:
            return True
        regular_reason = conn.execute(
            select(reason_runs.c.id)
            .where(
                reason_runs.c.task_id == task_id,
                reason_runs.c.status.in_(
                    ["pending", "claimed", "running", "concluding"]
                ),
                reason_runs.c.trigger != "plan",
            )
            .limit(1)
        ).scalar_one_or_none()
        if regular_reason is not None:
            return True
        open_explore = conn.execute(
            select(intents.c.id)
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "explore",
                intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                intents.c.archived.is_(False),
            )
            .limit(1)
        ).scalar_one_or_none()
        if open_explore is not None:
            return True
        if self._unconsumed_fact_ids(conn, task_id):
            return True
        snapshot = self._current_snapshot(conn, task_id)
        return bool(
            snapshot["failed_intent_ids"]
            or snapshot["actionable_todo_ids"]
            or snapshot["todo_review_ids"]
            or snapshot["unassessed_intent_ids"]
        )

    def _plan_allowed_in(self, conn, task, *, now: str) -> bool:
        """Apply the per-cycle convergence guard for reconciliation-created Plans."""
        cycle = task.current_goal_cycle_id
        # Keep the query local to this module without importing Goal/Reason
        # services.  A verified vulnerability resets the Plan budget.
        from axiom.modules.vulnerabilities.tables import vulnerabilities

        verified_at = conn.execute(
            select(vulnerabilities.c.updated_at)
            .where(
                vulnerabilities.c.task_id == task.id,
                vulnerabilities.c.status == "已验证",
            )
            .order_by(vulnerabilities.c.updated_at.desc(), vulnerabilities.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        conditions = [
            plan_runs.c.task_id == task.id,
            plan_runs.c.status.in_(["completed", "failed"]),
        ]
        if cycle is not None:
            conditions.append(plan_runs.c.goal_cycle_id == cycle)
        if verified_at is not None:
            conditions.append(plan_runs.c.created_at > verified_at)
        rows = conn.execute(
            select(plan_runs.c.id, plan_runs.c.status, plan_runs.c.failure_class)
            .where(*conditions)
            .order_by(plan_runs.c.created_at, plan_runs.c.id)
        ).mappings().all()
        # 若最近一次 PlanRun 提交了 exhausted 决策（确认全盘穷尽，没有新材料方向），
        # 则不再自动产生下一轮 Plan，直接保持 waiting；
        # 若前序 Plan 派发了 explore（dispatch_explore）并在 Explore/Reason 评估后再次进入死局，
        # 则允许继续进行最多 plan_max_consecutive_runs 轮连续 Plan 递进深挖。
        last_decision = conn.execute(
            select(plan_runs.c.decision_json)
            .where(plan_runs.c.task_id == task.id, plan_runs.c.status == "completed")
            .order_by(plan_runs.c.concluded_at.desc().nulls_last(), plan_runs.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last_decision:
            try:
                dec_data = json.loads(last_decision)
                if dec_data.get("decision") == "exhausted":
                    return False
            except Exception:
                pass

        # Infrastructure failures are retryable and do not count as a
        # convergence pass; execution failures do, because they represent an
        # exhausted/blocked Plan decision.
        effective = [row for row in rows if row["status"] == "completed" or row["failure_class"] != "transient"]
        config = load_runtime_config(conn)
        max_consecutive = max(1, int(getattr(config, "plan_max_consecutive_runs", 3)))
        if len(effective) >= max_consecutive:
            return False
        return True

    def _has_terminal_transient_failure_in(self, conn, task) -> bool:
        """Return whether automatic Plan recovery is blocked by a terminal infra failure.

        New releases keep retryable infrastructure failures on the same
        pending row and the claim path pauses that row when its budget is
        exhausted. This check remains for legacy rows that were persisted as
        terminal transient failures before the pause semantics shipped; those
        rows must not cause reconciliation to create replacement PlanRuns on
        every dispatcher tick.
        """
        conditions = [
            plan_runs.c.task_id == task.id,
            plan_runs.c.status == "failed",
            plan_runs.c.failure_class == "transient",
            plan_runs.c.concluded_at.is_not(None),
        ]
        cycle = task.current_goal_cycle_id
        if cycle is not None:
            conditions.append(plan_runs.c.goal_cycle_id == cycle)

        # Match the convergence guard's vulnerability reset semantics: a
        # verified vulnerability after the failed Plan opens a new planning
        # window, so an old infrastructure failure cannot strand the task.
        from axiom.modules.vulnerabilities.tables import vulnerabilities

        verified_at = conn.execute(
            select(vulnerabilities.c.updated_at)
            .where(
                vulnerabilities.c.task_id == task.id,
                vulnerabilities.c.status == "已验证",
            )
            .order_by(vulnerabilities.c.updated_at.desc(), vulnerabilities.c.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if verified_at is not None:
            conditions.append(plan_runs.c.created_at > verified_at)
        return (
            conn.execute(select(plan_runs.c.id).where(*conditions).limit(1)).scalar_one_or_none()
            is not None
        )

    def trigger(
        self,
        task_id: str,
        *,
        actor: str = "dispatcher.plan",
        trigger: str = "manual",
        trigger_payload: dict[str, Any] | None = None,
    ) -> PlanRun:
        now = utcnow()

        def operation(conn):
            task_lock = conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()).scalar_one_or_none()
            if task_lock is None:
                raise NotFoundError(f"Task not found: {task_id}")
            task = self.tasks.get(conn, task_id)
            if task is None or task.legacy_readonly or task.archived:
                raise ConflictError("Task is not writable")
            if task.status != "running":
                raise ConflictError("Only running tasks can trigger Plan")
            payload = dict(trigger_payload or {})
            payload.update({"actor": actor, "requested_at": now})
            snapshot = self._current_snapshot(conn, task_id)
            source_ids = snapshot["source_fact_ids"]
            payload.update({key: list(value) for key, value in snapshot.items()})
            pending = self.repository.pending_runs_for_task(conn, task_id)
            fingerprint = self._snapshot_fingerprint(snapshot)
            same = next((item for item in pending if item.input_fingerprint == fingerprint), None)
            if same is not None:
                merged_payload = self._merge_trigger_payload(
                    same.trigger_payload,
                    payload,
                    trigger=trigger,
                    existing_trigger=same.trigger,
                )
                merged_snapshot = self._merge_snapshots(
                    self._snapshot_from_run(same), snapshot
                )
                merged_payload.update(
                    {key: list(value) for key, value in merged_snapshot.items()}
                )
                merged_payload["source_fact_ids"] = list(merged_snapshot["source_fact_ids"])
                if not self.repository.update_pending_snapshot(
                    conn,
                    task_id=task_id,
                    run_id=same.id,
                    source_fact_ids=merged_snapshot["source_fact_ids"],
                    trigger_payload=merged_payload,
                    input_fingerprint=same.input_fingerprint,
                    now=now,
                ):
                    raise ConflictError("Pending PlanRun changed while coalescing a trigger")
                self._audit_in(
                    conn,
                    task=task,
                    plan_run_id=same.id,
                    event_type="plan_run.coalesced",
                    message="相同输入的 Plan 触发已合并到排队项",
                    payload={
                        "trigger": trigger,
                        "input_fingerprint": fingerprint,
                        "coalesced_count": merged_payload["coalesced_count"],
                    },
                )
                return self.repository.get_run(conn, task_id, same.id) or same
            config = load_runtime_config(conn)
            # Plan has its own FIFO queue.  When the queue is full, coalesce
            # into the tail while retaining the latest trigger metadata.
            limit = max(1, int(getattr(config, "plan_queue_limit", 3)))
            if len(pending) >= limit and pending:
                tail = pending[-1]
                merged_snapshot = self._merge_snapshots(self._snapshot_from_run(tail), snapshot)
                merged = self._merge_trigger_payload(
                    tail.trigger_payload,
                    payload,
                    trigger=trigger,
                    existing_trigger=tail.trigger,
                )
                merged.update({key: list(value) for key, value in merged_snapshot.items()})
                merged["source_fact_ids"] = list(merged_snapshot["source_fact_ids"])
                merged_fp = self._snapshot_fingerprint(merged_snapshot)
                if not self.repository.update_pending_snapshot(
                    conn,
                    task_id=task_id,
                    run_id=tail.id,
                    source_fact_ids=merged["source_fact_ids"],
                    trigger_payload=merged,
                    input_fingerprint=merged_fp,
                    now=now,
                ):
                    raise ConflictError("Pending PlanRun changed while coalescing a trigger")
                self._audit_in(
                    conn,
                    task=task,
                    plan_run_id=tail.id,
                    event_type="plan_run.coalesced",
                    message="新的 Plan 触发已合并到队尾排队项",
                    payload={
                        "trigger": trigger,
                        "queue_limit": limit,
                        "coalesced_count": merged["coalesced_count"],
                    },
                )
                return self.repository.get_run(conn, task_id, tail.id) or tail
            settings_version = conn.execute(
                select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
            ).scalar_one_or_none() or 1
            run = self.repository.create_run(
                conn,
                task_id=task_id,
                goal_cycle_id=task.current_goal_cycle_id,
                source_fact_ids=source_ids,
                trigger=trigger,
                trigger_payload={**payload, "source_fact_ids": source_ids},
                input_fingerprint=fingerprint,
                worker_pool=task.worker_pool,
                settings_version=int(settings_version),
                execute_timeout_seconds=getattr(config, "plan_execute_seconds", 600),
                conclude_timeout_seconds=getattr(config, "plan_conclude_seconds", 300),
                now=now,
            )
            # A manual Plan may be queued while a Reason/Explore owner is
            # still active.  Keep the task's visible phase truthful until the
            # existing owner has released it; the dispatcher will claim the
            # Plan FIFO head later.
            if not self._planning_blocked(conn, task_id):
                conn.execute(update(tasks).where(tasks.c.id == task_id).values(phase="plan", updated_at=now))
            self._audit_in(
                conn,
                task=task,
                plan_run_id=run.id,
                event_type="plan_run.created",
                message="已创建独立 PlanRun，等待 Plan Worker 领取",
                payload={"trigger": trigger, **payload, "input_fingerprint": fingerprint},
            )
            return run

        return run_transaction(self.engine, operation)

    def reconcile_task(self, task_id: str) -> PlanRun | None:
        now = utcnow()

        def operation(conn):
            if conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()).scalar_one_or_none() is None:
                return None
            task = self.tasks.get(conn, task_id)
            if task is None or task.status != "running" or task.archived or task.legacy_readonly:
                return None
            return self.reconcile_task_in(conn, task_id=task_id, now=now)

        return run_transaction(self.engine, operation)

    def trigger_in(self, conn, *, task, actor: str, trigger: str, now: str) -> PlanRun:
        snapshot = self._current_snapshot(conn, task.id)
        payload = {"actor": actor, "requested_at": now, "reconciled": True}
        payload.update({key: list(value) for key, value in snapshot.items()})
        source_ids = snapshot["source_fact_ids"]
        config = load_runtime_config(conn)
        settings_version = conn.execute(
            select(runtime_settings.c.version).where(runtime_settings.c.id == "runtime")
        ).scalar_one_or_none() or 1
        run = self.repository.create_run(
            conn,
            task_id=task.id,
            goal_cycle_id=task.current_goal_cycle_id,
            source_fact_ids=source_ids,
            trigger=trigger,
            trigger_payload={**payload, "source_fact_ids": source_ids},
            input_fingerprint=self._snapshot_fingerprint(snapshot),
            worker_pool=task.worker_pool,
            settings_version=int(settings_version),
            execute_timeout_seconds=getattr(config, "plan_execute_seconds", 600),
            conclude_timeout_seconds=getattr(config, "plan_conclude_seconds", 300),
            now=now,
        )
        conn.execute(update(tasks).where(tasks.c.id == task.id).values(phase="plan", updated_at=now))
        self._audit_in(
            conn,
            task=task,
            plan_run_id=run.id,
            event_type="plan_run.created",
            message="Reconciliation 已创建独立 PlanRun",
            payload={"trigger": trigger, **payload, "input_fingerprint": run.input_fingerprint},
        )
        return run

    def reconcile_all(self) -> PlanReconcileReport:
        created: list[str] = []
        now = utcnow()
        with transaction(self.engine) as conn:
            ids = list(conn.execute(select(tasks.c.id).where(tasks.c.status == "running", tasks.c.archived.is_(False), tasks.c.legacy_readonly.is_(False))).scalars())
            for task_id in ids:
                run = self.reconcile_task_in(conn, task_id=task_id, now=now)
                if run and run.created_at == now:
                    created.append(run.id)
        return PlanReconcileReport(created_plan_run_ids=created)

    def reconcile_task_in(self, conn, *, task_id: str, now: str) -> PlanRun | None:
        # ``reconcile_all`` shares one transaction across many tasks and does
        # not lock each row while collecting the candidate ids.  Lock here,
        # just like Reason reconciliation does, so two dispatcher ticks cannot
        # both observe an idle Task and create duplicate pending PlanRuns.
        if conn.execute(
            select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
        ).scalar_one_or_none() is None:
            return None
        task = self.tasks.get(conn, task_id)
        if task is None or task.status != "running" or task.archived or task.legacy_readonly:
            return None
        active = self.repository.in_flight_for_task(conn, task_id)
        if active:
            return active
        pending = self.repository.pending_runs_for_task(conn, task_id)
        if pending:
            if self._planning_blocked(conn, task_id):
                return None
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(phase="plan", updated_at=now))
            return pending[0]
        if self._planning_blocked(conn, task_id):
            return None
        if self._has_terminal_transient_failure_in(conn, task):
            # A terminal infrastructure failure is recoverable, but it must
            # not be converted to waiting or cause an unbounded stream of
            # replacement PlanRuns.  Manual ``plan-runs/trigger`` remains the
            # explicit recovery path.
            if task.phase != "plan":
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == task_id)
                    .values(phase="plan", updated_at=now)
                )
            return None
        if not self._plan_allowed_in(conn, task, now=now):
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(phase="waiting", updated_at=now))
            return None
        return self.trigger_in(conn, task=task, actor="dispatcher.reconcile", trigger="reconcile", now=now)

    def get_run(self, task_id: str, plan_run_id: str) -> PlanRun:
        with self.engine.connect() as conn:
            run = self.repository.get_run(conn, task_id, plan_run_id)
        if run is None:
            raise NotFoundError(f"PlanRun not found: {task_id}/{plan_run_id}")
        return run

    def claim_run(self, task_id: str, plan_run_id: str, *, worker_name: str, lease_id: str) -> PlanRun:
        now = utcnow()
        with transaction(self.engine) as conn:
            if conn.execute(select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()).scalar_one_or_none() is None:
                raise NotFoundError(f"Task not found: {task_id}")
            run = self.repository.get_run(conn, task_id, plan_run_id)
            if run is None:
                raise NotFoundError(f"PlanRun not found: {task_id}/{plan_run_id}")
            reason_in_flight = conn.execute(
                select(reason_runs.c.id)
                .where(
                    reason_runs.c.task_id == task_id,
                    reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                    reason_runs.c.trigger != "plan",
                )
                .limit(1)
            ).scalar_one_or_none()
            if reason_in_flight is not None:
                raise ConflictError("Task already has an active ReasonRun")
            reason_pending = conn.execute(
                select(reason_runs.c.id)
                .where(
                    reason_runs.c.task_id == task_id,
                    reason_runs.c.status == "pending",
                    reason_runs.c.trigger != "plan",
                )
                .order_by(reason_runs.c.created_at, reason_runs.c.id)
                .limit(1)
            ).scalar_one_or_none()
            if reason_pending is not None:
                raise ConflictError(
                    f"Task has a pending ReasonRun {reason_pending}; Plan must wait"
                )
            if self.repository.in_flight_for_task(conn, task_id) is not None:
                raise ConflictError("Task already has an active PlanRun")
            pending = self.repository.pending_runs_for_task(conn, task_id)
            if not pending or pending[0].id != plan_run_id:
                raise ConflictError("Only the FIFO head PlanRun can be claimed")
            config = load_runtime_config(conn)
            if not self.repository.claim(conn, task_id=task_id, run_id=plan_run_id, worker_name=worker_name, lease_id=lease_id, settings_version=run.settings_version, execute_timeout_seconds=getattr(config, "plan_execute_seconds", 600), conclude_timeout_seconds=getattr(config, "plan_conclude_seconds", 300), now=now):
                raise ConflictError("PlanRun claim lost a concurrent race")
            return self.repository.get_run(conn, task_id, plan_run_id) or run

    def start_run(self, task_id: str, plan_run_id: str, *, lease_id: str) -> StartedPlanRun:
        now = utcnow()
        with transaction(self.engine) as conn:
            if conn.execute(
                select(tasks.c.id).where(tasks.c.id == task_id).with_for_update()
            ).scalar_one_or_none() is None:
                raise NotFoundError(f"Task not found: {task_id}")
            run = self._require_lease(conn, task_id, plan_run_id, lease_id)
            reason_in_flight = conn.execute(
                select(reason_runs.c.id)
                .where(
                    reason_runs.c.task_id == task_id,
                    reason_runs.c.status.in_(["claimed", "running", "concluding"]),
                    reason_runs.c.trigger != "plan",
                )
                .limit(1)
            ).scalar_one_or_none()
            if reason_in_flight is not None:
                raise ConflictError("Task already has an active ReasonRun")
            reason_pending = conn.execute(
                select(reason_runs.c.id)
                .where(
                    reason_runs.c.task_id == task_id,
                    reason_runs.c.status == "pending",
                    reason_runs.c.trigger != "plan",
                )
                .order_by(reason_runs.c.created_at, reason_runs.c.id)
                .limit(1)
            ).scalar_one_or_none()
            if reason_pending is not None:
                raise ConflictError(
                    f"Task has a pending ReasonRun {reason_pending}; Plan must wait"
                )
            if run.status == "running":
                return StartedPlanRun(task_id=task_id, plan_run_id=plan_run_id, worker_name=run.worker or "", lease_id=lease_id)
            if run.status != "claimed":
                raise ConflictError("PlanRun is not in claimed state")
            # Claim and start are separate control-plane calls.  An Explore
            # or Bootstrap Intent can therefore arrive after the Plan claim
            # but before the worker starts.  Re-check the higher-priority
            # incremental owner under the Task lock so a Plan cannot begin
            # alongside work that appeared in that gap.
            incremental_owner = conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == task_id,
                    intents.c.kind.in_(("bootstrap", "explore")),
                    intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                    intents.c.archived.is_(False),
                )
                .order_by(intents.c.created_at, intents.c.id)
                .limit(1)
            ).scalar_one_or_none()
            if incremental_owner is not None:
                raise ConflictError(
                    f"Task has pending or active Explore/Bootstrap Intent {incremental_owner}; Plan must wait"
                )
            run = self._refresh_snapshot_in(conn, task_id=task_id, run=run, now=now)
            if not self.repository.start(conn, task_id=task_id, run_id=plan_run_id, lease_id=lease_id, now=now):
                raise ConflictError("PlanRun start lost a concurrent race")
            return StartedPlanRun(task_id=task_id, plan_run_id=plan_run_id, worker_name=run.worker or "", lease_id=lease_id)

    def heartbeat_run(self, task_id: str, plan_run_id: str, *, lease_id: str) -> None:
        with transaction(self.engine) as conn:
            if not self.repository.heartbeat(conn, task_id=task_id, run_id=plan_run_id, lease_id=lease_id, now=utcnow()):
                raise LeaseInactiveError("PlanRun lease is no longer active")

    def activity_run(self, task_id: str, plan_run_id: str, *, lease_id: str, activity_kind: str, activity_summary: str, activity_tool: str) -> None:
        with transaction(self.engine) as conn:
            if not self.repository.update_activity(conn, task_id=task_id, run_id=plan_run_id, lease_id=lease_id, activity_kind=activity_kind, activity_summary=activity_summary, activity_tool=activity_tool, now=utcnow()):
                raise LeaseInactiveError("PlanRun lease is no longer active")

    def begin_conclude_run(self, task_id: str, plan_run_id: str, *, lease_id: str) -> None:
        with transaction(self.engine) as conn:
            if not self.repository.begin_conclude(conn, task_id=task_id, run_id=plan_run_id, lease_id=lease_id, now=utcnow()):
                raise ConflictError("PlanRun is not running under this lease")

    def release_run(self, task_id: str, plan_run_id: str, *, lease_id: str, failure_kind: str, failure_class: str) -> ReleasedPlanRun:
        now = utcnow()
        with transaction(self.engine) as conn:
            run = self._require_lease(conn, task_id, plan_run_id, lease_id)
            config = load_runtime_config(conn)
            count = run.retry_count + 1 if failure_class == "execution" else run.infrastructure_retry_count + 1
            outcome = retry_outcome(config, attempt=count, failure_class=failure_class, now=now)
            terminal = outcome.terminal
            retry_at = outcome.next_retry_at
            if not self.repository.release(conn, task_id=task_id, run_id=plan_run_id, lease_id=lease_id, failure_kind=failure_kind, failure_class=failure_class, next_retry_at=retry_at, terminal=terminal, now=now):
                raise LeaseInactiveError("PlanRun lease is no longer active")
            worker_pool = (
                narrow_pool_after_failure(
                    conn,
                    table=plan_runs,
                    run_id=plan_run_id,
                    task_id=task_id,
                    pool_json=run.worker_pool,
                    failed_worker=run.worker,
                    kind="plan",
                    now=now,
                )
                if failure_class == "transient"
                else json_list(run.worker_pool)
            )
            self.repository.abandon_open(conn, task_id=task_id, plan_run_id=plan_run_id, lease_id=lease_id, failure_reason=failure_kind or "plan_run_released", now=now)
            task = self.tasks.get(conn, task_id)
            if task is not None:
                self._audit_in(
                    conn,
                    task=task,
                    plan_run_id=plan_run_id,
                    worker=run.worker or "",
                    event_type="plan_run.released",
                    message=f"PlanRun 已释放：{failure_kind or '未指定原因'}",
                    payload={
                        "failure_kind": failure_kind,
                        "failure_class": failure_class,
                        "terminal": outcome.terminal,
                        "retry_blocked": outcome.retry_blocked,
                        "next_retry_at": retry_at,
                        "worker_pool": worker_pool,
                    },
                )
                if outcome.retry_blocked:
                    self._audit_in(
                        conn,
                        task=task,
                        plan_run_id=plan_run_id,
                        worker=run.worker or "",
                        level="warning",
                        event_type="plan_run.retry_blocked",
                        message=retry_blocked_message("plan", failure_kind),
                        payload={
                            "failure_kind": failure_kind,
                            "infrastructure_retry_count": outcome.attempt,
                            "limit": outcome.limit,
                            "failed_worker": run.worker or "",
                            "worker_pool": worker_pool,
                        },
                    )
            result = ReleasedPlanRun(task_id=task_id, plan_run_id=plan_run_id, status="failed" if terminal else "pending", next_retry_at=retry_at, retry_count=run.retry_count + (1 if failure_class == "execution" else 0), infrastructure_retry_count=run.infrastructure_retry_count + (1 if failure_class == "transient" else 0))
        if self.sandboxes is not None:
            try:
                self.sandboxes.cleanup_run_directory(
                    task_id=task_id, owner_id=plan_run_id, lease_id=lease_id,
                )
            except OSError:
                logger.exception(
                    "failed to clean released run dir: %s/%s", task_id, plan_run_id
                )
        return result

    def _require_lease(self, conn, task_id: str, run_id: str, lease_id: str) -> PlanRun:
        run = self.repository.get_run(conn, task_id, run_id)
        if run is None:
            raise NotFoundError(f"PlanRun not found: {task_id}/{run_id}")
        if run.lease_id != lease_id:
            raise LeaseInactiveError("PlanRun lease does not match")
        return run

    # Workspace operations -------------------------------------------------
    def open(self, *, task_id: str, plan_run_id: str, lease_id: str, context_revision: int) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            run = self._require_lease(conn, task_id, plan_run_id, lease_id)
            if run.status not in {"running", "concluding"}:
                raise ConflictError("PlanRun is not active")
            existing = self.repository.for_attempt(conn, task_id, plan_run_id, lease_id)
            if existing is not None:
                return existing
            context = conn.execute(
                select(context_revisions.c.revision).where(
                    context_revisions.c.task_id == task_id,
                    context_revisions.c.revision == context_revision,
                    context_revisions.c.plan_run_id == plan_run_id,
                    context_revisions.c.lease_id == lease_id,
                    context_revisions.c.phase == "plan",
                )
            ).scalar_one_or_none()
            if context is None:
                raise ConflictError("Plan Context does not match this lease")
            workspace = self.repository.open(
                conn,
                task_id=task_id,
                plan_run_id=plan_run_id,
                lease_id=lease_id,
                worker=run.worker or "",
                context_revision=context_revision,
                now=utcnow(),
            )
            task = self.tasks.get(conn, task_id)
            if task is not None:
                self._audit_in(
                    conn,
                    task=task,
                    plan_run_id=plan_run_id,
                    workspace_id=workspace.id,
                    worker=run.worker or "",
                    event_type="plan.workspace_opened",
                    message="Plan Workspace 已打开",
                    payload={"context_revision": context_revision, "lease_id": lease_id},
                )
            return workspace

    def get(self, workspace_id: str) -> PlanWorkspace:
        with self.engine.connect() as conn:
            workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Plan Workspace not found: {workspace_id}")
        return workspace

    def _active_workspace(self, conn, workspace_id: str, lease_id: str) -> PlanWorkspace:
        workspace = self.repository.get(conn, workspace_id)
        if workspace is None:
            raise NotFoundError(f"Plan Workspace not found: {workspace_id}")
        if workspace.lease_id != lease_id:
            raise LeaseInactiveError("Plan Workspace lease does not match")
        run = self._require_lease(conn, workspace.task_id, workspace.plan_run_id, lease_id)
        if workspace.status != "open" or run.status not in LEASED_PLAN_STATUSES:
            raise ConflictError("Plan Workspace is not open")
        return workspace

    def abandon(self, workspace_id: str, *, lease_id: str, failure_reason: str) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            result = self.repository.abandon(conn, workspace_id, failure_reason=failure_reason.strip() or "Plan execution failed", now=utcnow())
            task = self.tasks.get(conn, workspace.task_id)
            if task is not None:
                self._audit_in(
                    conn,
                    task=task,
                    plan_run_id=workspace.plan_run_id,
                    workspace_id=workspace.id,
                    worker=workspace.worker,
                    event_type="plan.workspace_abandoned",
                    message="Plan Workspace 已放弃",
                    payload={"failure_reason": failure_reason.strip() or "Plan execution failed"},
                )
            return result

    def add_todo(self, workspace_id: str, todo: DraftPlanTodo, *, lease_id: str) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            if any(item.ref == todo.ref for item in workspace.draft.todo_adds):
                raise ConflictError(f"Todo draft ref already exists: {todo.ref}")
            result = self.repository.save_draft(conn, workspace_id, workspace.draft.model_copy(update={"todo_adds": [*workspace.draft.todo_adds, todo]}), now=utcnow())
            task = self.tasks.get(conn, workspace.task_id)
            if task is not None:
                self._audit_in(conn, task=task, plan_run_id=workspace.plan_run_id, workspace_id=workspace.id, worker=workspace.worker, event_type="plan.todo_added", message="Plan 新增 Todo 草案", payload=todo.model_dump(mode="json"))
            return result

    def update_todo(self, workspace_id: str, update_command: DraftPlanTodoUpdate, *, lease_id: str) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            if not any(item.ref == update_command.todo_id for item in workspace.draft.todo_adds) and self.todos.get(conn, workspace.task_id, update_command.todo_id) is None:
                raise NotFoundError(f"Todo not found: {update_command.todo_id}")
            updates = [item for item in workspace.draft.todo_updates if item.todo_id != update_command.todo_id]
            updates.append(update_command)
            result = self.repository.save_draft(conn, workspace_id, workspace.draft.model_copy(update={"todo_updates": updates}), now=utcnow())
            task = self.tasks.get(conn, workspace.task_id)
            if task is not None:
                self._audit_in(conn, task=task, plan_run_id=workspace.plan_run_id, workspace_id=workspace.id, worker=workspace.worker, event_type="plan.todo_updated", message="Plan 更新 Todo 草案", payload=update_command.model_dump(mode="json"))
            return result

    def delete_todo(self, workspace_id: str, deletion: DraftPlanTodoDelete, *, lease_id: str) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            todo_adds = [item for item in workspace.draft.todo_adds if item.ref != deletion.todo_id]
            deletions = [item for item in workspace.draft.todo_deletions if item.todo_id != deletion.todo_id]
            if self.todos.get(conn, workspace.task_id, deletion.todo_id) is not None:
                deletions.append(deletion)
            draft = workspace.draft.model_copy(update={"todo_adds": todo_adds, "todo_deletions": deletions})
            result = self.repository.save_draft(conn, workspace_id, draft, now=utcnow())
            task = self.tasks.get(conn, workspace.task_id)
            if task is not None:
                self._audit_in(conn, task=task, plan_run_id=workspace.plan_run_id, workspace_id=workspace.id, worker=workspace.worker, event_type="plan.todo_obsoleted", message="Plan 将 Todo 标记为 obsolete", payload=deletion.model_dump(mode="json"))
            return result

    def propose_intent(self, workspace_id: str, proposal: DraftPlanIntent, *, lease_id: str) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            requested_execute_seconds = proposal.execute_seconds
            requested_conclude_seconds = proposal.conclude_seconds
            budget = resolve_explore_budget(
                load_runtime_config(conn),
                execute_seconds=requested_execute_seconds,
                conclude_seconds=requested_conclude_seconds,
            )
            proposal = proposal.model_copy(
                update={
                    "execute_seconds": budget.execute_seconds,
                    "conclude_seconds": budget.conclude_seconds,
                }
            )
            computed = self.intent_fingerprint(proposal)
            identity = computed
            if any((item.dedup_fingerprint or self.intent_fingerprint(item)) == identity for item in workspace.draft.intent_proposals):
                raise ConflictError("Plan Workspace 中已经存在相同 fingerprint 的 Intent")
            if len(workspace.draft.intent_proposals) >= MAX_PLAN_INTENTS:
                raise ConflictError(f"Plan Workspace Intent infrastructure limit reached: {MAX_PLAN_INTENTS}")
            proposal = proposal.model_copy(update={"dedup_fingerprint": computed})
            self._validate_intent_proposal(conn, workspace, proposal)
            if proposal.ref and any(item.ref == proposal.ref for item in workspace.draft.intent_proposals):
                raise ConflictError(f"Intent draft ref already exists: {proposal.ref}")
            result = self.repository.save_draft(conn, workspace_id, workspace.draft.model_copy(update={"intent_proposals": [*workspace.draft.intent_proposals, proposal]}), now=utcnow())
            task = self.tasks.get(conn, workspace.task_id)
            if task is not None:
                if budget.used_legacy_values:
                    self._audit_in(
                        conn,
                        task=task,
                        plan_run_id=workspace.plan_run_id,
                        workspace_id=workspace.id,
                        worker=workspace.worker,
                        event_type="plan.intent_budget_normalized",
                        message="Plan Intent 的旧时间字段已由后端配置覆盖",
                        payload={
                            "legacy_execute_seconds": requested_execute_seconds,
                            "legacy_conclude_seconds": requested_conclude_seconds,
                            "execute_timeout_seconds": budget.execute_seconds,
                            "conclude_timeout_seconds": budget.conclude_seconds,
                            "legacy_values_ignored": True,
                        },
                    )
                self._audit_in(conn, task=task, plan_run_id=workspace.plan_run_id, workspace_id=workspace.id, worker=workspace.worker, event_type="plan.intent_proposed", message="Plan 提出 Explore Intent 草案", payload=proposal.model_dump(mode="json"))
            return result

    @staticmethod
    def intent_fingerprint(proposal: DraftPlanIntent) -> str:
        return hashlib.sha256(json.dumps({"target": sorted(set(proposal.targets)), "endpoint": proposal.endpoint.strip(), "test_vector": (proposal.test_vector or proposal.description).strip()}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def submit_decision(self, workspace_id: str, decision: PlanDecision, *, lease_id: str) -> PlanWorkspace:
        if decision.decision == "exhausted" and not decision.exhaustion_reason.strip():
            raise ConflictError("exhausted Plan decision requires exhaustion_reason")
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            result = self.repository.save_draft(conn, workspace_id, workspace.draft.model_copy(update={"decision": decision}), now=utcnow())
            task = self.tasks.get(conn, workspace.task_id)
            if task is not None:
                self._audit_in(conn, task=task, plan_run_id=workspace.plan_run_id, workspace_id=workspace.id, worker=workspace.worker, event_type="plan.decision_submitted", message="Plan 决策已提交", payload=decision.model_dump(mode="json"))
            return result

    def _validate_sources(self, conn, task_id: str, sources) -> None:
        for source in sources:
            if source.type == "fact" and source.id == "goal":
                raise ConflictError("Goal Fact cannot be used as a Todo source")
            if not self.todos.source_exists(conn, task_id=task_id, source=source):
                raise ConflictError(f"Todo source not found: {source.type}:{source.id}")

    def _validate_intent_proposal(self, conn, workspace: PlanWorkspace, proposal: DraftPlanIntent) -> None:
        task = self.tasks.get(conn, workspace.task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {workspace.task_id}")
        draft = workspace.draft
        if proposal.todo_ref:
            todo = next((item for item in draft.todo_adds if item.ref == proposal.todo_ref), None)
            if todo is None:
                raise ConflictError(f"Intent references unknown Todo draft: {proposal.todo_ref}")
            staged = next((item for item in draft.todo_updates if item.todo_id == proposal.todo_ref), None)
            effective_status = staged.status if staged and staged.status is not None else todo.status
        else:
            todo = self.todos.get(conn, workspace.task_id, proposal.todo_id or "")
            if todo is None:
                raise NotFoundError(f"Todo not found: {proposal.todo_id}")
            if todo.archived:
                raise ConflictError(f"Intent cannot reference archived Todo: {todo.id}")
            staged = next((item for item in draft.todo_updates if item.todo_id == todo.id), None)
            effective_status = staged.status if staged and staged.status is not None else todo.status
        if any(item.todo_id == (proposal.todo_ref or proposal.todo_id) for item in draft.todo_deletions):
            raise ConflictError(f"Intent cannot reference an obsoleted Todo: {proposal.todo_ref or proposal.todo_id}")
        if effective_status in {"blocked", "completed", "rejected", "obsolete"}:
            raise ConflictError(f"Intent cannot reference terminal Todo: {proposal.todo_ref or proposal.todo_id}")
        if "goal" in proposal.from_facts:
            raise ConflictError("Goal Fact cannot be used as an Intent source")
        if proposal.from_facts:
            found = set(
                conn.execute(
                    select(facts.c.id).where(
                        facts.c.task_id == workspace.task_id,
                        facts.c.id.in_(proposal.from_facts),
                        facts.c.archived.is_(False),
                    )
                ).scalars()
            )
            missing = [item for item in proposal.from_facts if item not in found]
            if missing:
                raise ConflictError("Intent source Fact not found: " + ", ".join(missing))
        for target in proposal.targets:
            if not self._scope_target_allowed(task, target):
                raise ConflictError(f"Intent target outside Task Scope: {target}")
        if task.scope.enabled and not proposal.targets:
            raise ConflictError("Intent targets are required when Task Scope is configured")
        config = load_runtime_config(conn)
        budget = resolve_explore_budget(
            config,
            execute_seconds=proposal.execute_seconds,
            conclude_seconds=proposal.conclude_seconds,
        )
        if not (
            config.explore_execute_min_seconds
            <= budget.execute_seconds
            <= config.explore_execute_max_seconds
        ):
            raise ConflictError(
                "Intent execute_seconds outside configured range: "
                f"{config.explore_execute_min_seconds}-"
                f"{config.explore_execute_max_seconds}"
            )
        if not (
            config.explore_conclude_min_seconds
            <= budget.conclude_seconds
            <= config.explore_conclude_max_seconds
        ):
            raise ConflictError(
                "Intent conclude_seconds outside configured range: "
                f"{config.explore_conclude_min_seconds}-"
                f"{config.explore_conclude_max_seconds}"
            )
        if not proposal.source_refs and not proposal.from_facts:
            raise ConflictError("Plan Intent requires at least one source reference")
        for reference in proposal.source_refs:
            kind, separator, item_id = reference.partition(":")
            if not separator or not item_id:
                raise ConflictError(f"Invalid Plan source reference: {reference}")
            if kind not in PLAN_SOURCE_REFERENCE_KINDS:
                raise ConflictError(f"Unsupported Plan source reference kind: {kind}")
            if kind == "fact":
                exists_ref = conn.execute(
                    select(facts.c.id).where(
                        facts.c.task_id == workspace.task_id,
                        facts.c.id == item_id,
                        facts.c.archived.is_(False),
                    )
                ).scalar_one_or_none()
            elif kind == "intent":
                exists_ref = conn.execute(
                    select(intents.c.id).where(
                        intents.c.task_id == workspace.task_id,
                        intents.c.id == item_id,
                        intents.c.archived.is_(False),
                    )
                ).scalar_one_or_none()
            elif kind == "todo":
                exists_ref = (
                    item_id
                    if item_id in {item.ref for item in draft.todo_adds}
                    else conn.execute(
                        select(todos.c.id).where(
                            todos.c.task_id == workspace.task_id,
                            todos.c.id == item_id,
                            todos.c.archived.is_(False),
                        )
                    ).scalar_one_or_none()
                )
            elif kind in {"artifact", "finding", "lead", "evidence"}:
                exists_ref = self._artifact_source_reference(
                    conn,
                    task_id=workspace.task_id,
                    kind=kind,
                    artifact_id=item_id,
                )
            elif kind == "hint":
                exists_ref = conn.execute(
                    select(task_hints.c.id).where(
                        task_hints.c.task_id == workspace.task_id,
                        task_hints.c.id == item_id,
                        task_hints.c.archived.is_(False),
                    )
                ).scalar_one_or_none()
            elif kind == "knowledge":
                exists_ref = conn.execute(
                    select(project_knowledge.c.id).where(
                        project_knowledge.c.project_id == task.project_id,
                        project_knowledge.c.id == item_id,
                        project_knowledge.c.archived.is_(False),
                    )
                ).scalar_one_or_none()
            else:
                exists_ref = conn.execute(
                    select(goal_evaluations.c.id).where(
                        goal_evaluations.c.task_id == workspace.task_id,
                        goal_evaluations.c.id == item_id,
                    )
                ).scalar_one_or_none()
            if exists_ref is None:
                raise ConflictError(f"Plan source reference not found: {reference}")
        self._reject_repeated_fingerprint(conn, workspace, proposal)

    @staticmethod
    def _artifact_source_reference(
        conn,
        *,
        task_id: str,
        kind: str,
        artifact_id: str,
    ) -> str | None:
        artifact_type = conn.execute(
            select(artifacts.c.artifact_type).where(
                artifacts.c.task_id == task_id,
                artifacts.c.id == artifact_id,
                artifacts.c.archived.is_(False),
            )
        ).scalar_one_or_none()
        if artifact_type is None:
            return None
        if kind == "artifact":
            return artifact_id
        normalized_type = artifact_type.strip().lower()
        if kind == "lead" and (
            normalized_type.endswith(".leads")
            or normalized_type == "bootstrap.initial_leads"
        ):
            return artifact_id
        if kind == "finding" and normalized_type.endswith(".findings"):
            return artifact_id
        if kind == "evidence" and normalized_type.endswith(".evidence"):
            return artifact_id
        if kind in {"finding", "evidence"}:
            role = conn.execute(
                select(vulnerability_artifacts.c.role)
                .where(
                    vulnerability_artifacts.c.task_id == task_id,
                    vulnerability_artifacts.c.artifact_id == artifact_id,
                    vulnerability_artifacts.c.role == kind,
                )
                .limit(1)
            ).scalar_one_or_none()
            if role is not None:
                return artifact_id
        return None

    def _reject_repeated_fingerprint(self, conn, workspace: PlanWorkspace, proposal: DraftPlanIntent) -> None:
        fingerprint = proposal.dedup_fingerprint or self.intent_fingerprint(proposal)
        new_facts = self._proposal_fact_refs(proposal)
        intent_rows = conn.execute(
            select(
                intents.c.id,
                intents.c.plan_metadata_json,
                intents.c.created_at,
                intents.c.concluded_at,
            )
            .where(
                intents.c.task_id == workspace.task_id,
                intents.c.dedup_fingerprint == fingerprint,
            )
            .order_by(intents.c.created_at.desc(), intents.c.id.desc())
        ).mappings()
        for row in intent_rows:
            try:
                metadata = json.loads(row["plan_metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            previous_facts = self._metadata_fact_refs(metadata)
            if not self._has_fresh_fact_since(
                conn,
                task_id=workspace.task_id,
                candidate_fact_ids=new_facts - previous_facts,
                created_after=row["concluded_at"] or row["created_at"],
            ):
                raise ConflictError("Plan Intent fingerprint 已存在且没有新 Fact")

        # Inspect committed Plan drafts rather than trying to reconstruct
        # metadata for legacy Plan-created Intents that predate the durable
        # fingerprint columns.
        rows = conn.execute(
            select(plan_workspaces.c.id, plan_workspaces.c.draft_json, plan_workspaces.c.committed_at)
            .where(
                plan_workspaces.c.task_id == workspace.task_id,
                plan_workspaces.c.status == "committed",
                plan_workspaces.c.id != workspace.id,
            )
        ).mappings()
        for row in rows:
            try:
                payload = json.loads(row["draft_json"] or "{}")
            except (TypeError, ValueError):
                continue
            for previous in payload.get("intent_proposals", []) or []:
                if previous.get("dedup_fingerprint") != fingerprint:
                    continue
                previous_facts = self._metadata_fact_refs(previous)
                if not self._has_fresh_fact_since(
                    conn,
                    task_id=workspace.task_id,
                    candidate_fact_ids=new_facts - previous_facts,
                    created_after=row["committed_at"],
                ):
                    raise ConflictError("Plan Intent fingerprint 已失败/完成且没有新 Fact")

    @staticmethod
    def _proposal_fact_refs(proposal: DraftPlanIntent) -> set[str]:
        return {
            *proposal.from_facts,
            *(
                item_id
                for reference in proposal.source_refs
                for kind, separator, item_id in [reference.partition(":")]
                if separator and kind == "fact" and item_id
            ),
        }

    @staticmethod
    def _metadata_fact_refs(metadata: dict[str, Any]) -> set[str]:
        return {
            *(metadata.get("from_facts", []) or []),
            *(
                item_id
                for reference in metadata.get("source_refs", []) or []
                for kind, separator, item_id in [str(reference).partition(":")]
                if separator and kind == "fact" and item_id
            ),
        }

    @staticmethod
    def _has_fresh_fact_since(
        conn,
        *,
        task_id: str,
        candidate_fact_ids: set[str],
        created_after: str | None,
    ) -> bool:
        if not candidate_fact_ids or created_after is None:
            return False
        return (
            conn.execute(
                select(facts.c.id)
                .where(
                    facts.c.task_id == task_id,
                    facts.c.id.in_(candidate_fact_ids),
                    facts.c.created_at > created_after,
                    facts.c.archived.is_(False),
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

    def _readiness_errors(self, conn, workspace: PlanWorkspace, run: PlanRun) -> list[str]:
        draft = workspace.draft
        errors: list[str] = []
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
                target = next((todo for todo in draft.todo_adds if todo.ref == item.todo_id), None)
                persisted = None if target is not None else self.todos.get(conn, workspace.task_id, item.todo_id)
                if target is None and persisted is None:
                    raise NotFoundError(f"Todo not found: {item.todo_id}")
                if persisted is not None and persisted.archived:
                    raise ConflictError(f"Todo is archived: {item.todo_id}")
                self._validate_sources(conn, workspace.task_id, item.source_adds)
                baseline_status = target.status if target is not None else persisted.status
                effective_status = item.status or baseline_status
                effective_reason = item.status_reason if item.status_reason is not None else (target.status_reason if target is not None else persisted.status_reason)
                if effective_status in TODO_STATUS_REASON_REQUIRED and not (effective_reason or "").strip():
                    raise ConflictError(f"status_reason is required for Todo status {effective_status}")
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
        for deletion in draft.todo_deletions:
            if deletion.todo_id in refs:
                continue
            todo = self.todos.get(conn, workspace.task_id, deletion.todo_id)
            if todo is None:
                errors.append(f"Todo not found: {deletion.todo_id}")
            elif todo.archived:
                errors.append(f"Todo is archived: {deletion.todo_id}")
        for proposal in draft.intent_proposals:
            try:
                self._validate_intent_proposal(conn, workspace, proposal)
            except (ConflictError, NotFoundError) as exc:
                errors.append(str(exc))
        decision = draft.decision
        if decision is None:
            errors.append("缺少 Plan decision")
        else:
            if decision.decision == "exhausted" and draft.intent_proposals:
                errors.append("exhausted Plan 不能同时派发 Explore Intent")
            if decision.decision == "needs_reason" and draft.intent_proposals:
                errors.append("needs_reason Plan 不能同时派发 Explore Intent")
            if decision.decision == "dispatch_explore" and not draft.intent_proposals:
                errors.append("dispatch_explore Plan 必须至少派发一个 Explore Intent")
            if decision.decision == "exhausted" and not decision.reviewed_dimensions:
                errors.append("exhausted Plan 必须填写 reviewed_dimensions")
            if decision.decision == "exhausted":
                if any(item.status in {"open", "partial", "blocked"} for item in draft.todo_adds):
                    errors.append("exhausted Plan 不能留下未收口的新增 Todo")
                staged_updates = {
                    item.todo_id: item
                    for item in draft.todo_updates
                    if item.todo_id not in {item.ref for item in draft.todo_adds}
                }
                deleted_ids = {item.todo_id for item in draft.todo_deletions}
                existing_open = conn.execute(
                    select(todos.c.id, todos.c.status)
                    .where(
                        todos.c.task_id == workspace.task_id,
                        todos.c.archived.is_(False),
                        todos.c.status.in_(("open", "partial", "blocked")),
                    )
                ).mappings()
                for row in existing_open:
                    if row["id"] in deleted_ids:
                        continue
                    update_command = staged_updates.get(row["id"])
                    effective_status = (
                        update_command.status
                        if update_command is not None and update_command.status is not None
                        else row["status"]
                    )
                    if effective_status in {"open", "partial", "blocked"}:
                        errors.append(f"exhausted Plan 不能留下未收口的 Todo: {row['id']}")
                for item in draft.todo_updates:
                    persisted = self.todos.get(conn, workspace.task_id, item.todo_id)
                    baseline = persisted.status if persisted is not None else "open"
                    if (item.status or baseline) in {"open", "partial", "blocked"}:
                        errors.append(f"exhausted Plan 不能留下未收口的 Todo: {item.todo_id}")
                reviewed_todo_ids = {
                    item.todo_id for item in draft.todo_updates
                } | {
                    item.todo_id for item in draft.todo_deletions
                }
                active_todo_ids = set(
                    conn.execute(
                        select(todos.c.id).where(
                            todos.c.task_id == workspace.task_id,
                            todos.c.status.in_(["open", "partial", "blocked"]),
                            todos.c.archived.is_(False),
                        )
                    ).scalars()
                )
                for todo_id in sorted(active_todo_ids - reviewed_todo_ids):
                    errors.append(
                        f"exhausted Plan 必须审查现有未收口 Todo: {todo_id}"
                    )
        return list(dict.fromkeys(errors))

    def add_chain(self, workspace_id: str, chain: DraftChain, *, lease_id: str) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            task = self._require_red_team(conn, workspace)
            self._validate_chain_facts(conn, workspace.task_id, chain.source_fact_ids)
            self._validate_chain_targets(task, chain)
            existing = next(
                (item for item in workspace.draft.chain_adds if item.ref == chain.ref),
                None,
            )
            if existing is not None:
                if existing == chain:
                    return workspace
                raise ConflictError(
                    f"Chain draft ref already exists with different content: {chain.ref}"
                )
            draft = workspace.draft.model_copy(
                update={"chain_adds": [*workspace.draft.chain_adds, chain]}
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def update_chain(
        self,
        workspace_id: str,
        update_command: DraftChainUpdate,
        *,
        lease_id: str,
    ) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            self._require_red_team(conn, workspace)
            # Updates target an already persisted Chain; staged refs are
            # resolved only by chain_intent_links at commit time.
            self.red_team.get_chain(workspace.task_id, update_command.chain_id)
            updates = [
                item
                for item in workspace.draft.chain_updates
                if item.chain_id != update_command.chain_id
            ]
            previous = next(
                (
                    item
                    for item in workspace.draft.chain_updates
                    if item.chain_id == update_command.chain_id
                ),
                None,
            )
            if previous is not None:
                merged = previous.model_dump()
                merged.update(update_command.model_dump(exclude_unset=True))
                update_command = DraftChainUpdate.model_validate(merged)
            updates.append(update_command)
            draft = workspace.draft.model_copy(update={"chain_updates": updates})
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def link_chain_intent(
        self,
        workspace_id: str,
        link: DraftChainIntentLink,
        *,
        lease_id: str,
    ) -> PlanWorkspace:
        with transaction(self.engine) as conn:
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            self._require_red_team(conn, workspace)
            draft = workspace.draft
            if link.chain_ref:
                if not any(item.ref == link.chain_ref for item in draft.chain_adds):
                    raise NotFoundError(f"Chain draft ref not found: {link.chain_ref}")
            else:
                self.red_team.get_chain(workspace.task_id, link.chain_id)
            if not any(item.ref for item in draft.intent_proposals if item.ref == link.intent_ref):
                raise NotFoundError(f"Intent draft ref not found: {link.intent_ref}")
            if any(item == link for item in draft.chain_intent_links):
                return workspace
            draft = draft.model_copy(
                update={"chain_intent_links": [*draft.chain_intent_links, link]}
            )
            return self.repository.save_draft(conn, workspace_id, draft, now=utcnow())

    def _require_red_team(self, conn, workspace: PlanWorkspace):
        task = self.tasks.get(conn, workspace.task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {workspace.task_id}")
        if task.engagement_mode != "red_team":
            raise ConflictError("RedTeam Chains are only available for red_team tasks")
        if self.red_team is None:
            raise ConflictError("Red-team service is not configured")
        return task

    @staticmethod
    def _validate_chain_targets(task, chain: DraftChain) -> None:
        objective = task.red_team_objective
        allowed_capabilities = set(objective.target_capabilities if objective else [])
        allowed_assets = set(objective.target_assets if objective else [])
        # 未声明结构化目标（空列表）= 不设上限；显式声明则锁死边界。
        if allowed_capabilities and not set(chain.target_capabilities).issubset(allowed_capabilities):
            raise ConflictError("Chain capability target exceeds the task objective")
        if allowed_assets and not set(chain.target_assets).issubset(allowed_assets):
            raise ConflictError("Chain asset target exceeds the task objective")

    def _validate_chain_facts(self, conn, task_id: str, fact_ids: list[str]) -> None:
        for fact_id in fact_ids:
            if fact_id == "goal" or not self.todos.source_exists(
                conn, task_id=task_id, source=TodoSource(type="fact", id=fact_id)
            ):
                raise ConflictError(f"Chain source Fact not found: {fact_id}")

    def status(self, workspace_id: str, *, lease_id: str) -> PlanWorkspaceStatus:
        with transaction(self.engine) as conn:
            workspace = self.repository.get(conn, workspace_id)
            if workspace is None:
                raise NotFoundError(f"Plan Workspace not found: {workspace_id}")
            if workspace.status == "open":
                self._active_workspace(conn, workspace_id, lease_id)
            run = self.repository.get_run(conn, workspace.task_id, workspace.plan_run_id)
            errors = (
                self._readiness_errors(conn, workspace, run)
                if workspace.status == "open" and run is not None
                else ["PlanRun not found"]
                if workspace.status == "open"
                else []
            )
            return PlanWorkspaceStatus(workspace_id=workspace.id, status=workspace.status, todo_add_count=len(workspace.draft.todo_adds), todo_update_count=len(workspace.draft.todo_updates), intent_proposal_count=len(workspace.draft.intent_proposals), chain_add_count=len(workspace.draft.chain_adds), chain_update_count=len(workspace.draft.chain_updates), chain_link_count=len(workspace.draft.chain_intent_links), chain_refs=[item.ref for item in workspace.draft.chain_adds], decision=workspace.draft.decision, ready=workspace.status == "committed" or not errors, errors=errors)

    def commit(self, workspace_id: str, *, lease_id: str) -> PlanCommitResult:
        with transaction(self.engine) as conn:
            workspace = self.repository.get(conn, workspace_id)
            if workspace is None:
                raise NotFoundError(f"Plan Workspace not found: {workspace_id}")
            if workspace.status == "committed":
                result = self.repository.commit_result(conn, workspace)
                return PlanCommitResult(workspace=workspace, **{key: result.get(key, []) for key in ("created_todo_ids", "updated_todo_ids", "deleted_todo_ids", "created_intent_ids", "created_chain_ids")})
            # A Plan commit is the hand-off point between the global review
            # phase and the normal Reason/Explore state machine.  Lock the
            # Task before validating the workspace or inspecting any queue so
            # that input arrival and reconciliation cannot race the phase
            # transition below.  The workspace ID is the only request input,
            # so resolve its task first, then acquire the canonical Task lock
            # used by all other lifecycle transitions.
            if conn.execute(
                select(tasks.c.id)
                .where(tasks.c.id == workspace.task_id)
                .with_for_update()
            ).scalar_one_or_none() is None:
                raise NotFoundError(f"Task not found: {workspace.task_id}")
            workspace = self._active_workspace(conn, workspace_id, lease_id)
            draft = workspace.draft
            if draft.decision is None:
                raise ConflictError("Plan Workspace 缺少 decision")
            task = self.tasks.get(conn, workspace.task_id)
            if task is None:
                raise NotFoundError(f"Task not found: {workspace.task_id}")
            run = self.repository.get_run(conn, workspace.task_id, workspace.plan_run_id)
            if run is None:
                raise NotFoundError(f"PlanRun not found: {workspace.task_id}/{workspace.plan_run_id}")
            readiness_errors = self._readiness_errors(conn, workspace, run)
            if readiness_errors:
                raise ConflictError("；".join(readiness_errors))
            now = utcnow()
            self._align_todo_counter(conn, workspace.task_id)
            todo_refs: dict[str, str] = {}
            created_todo_ids: list[str] = []
            updated_todo_ids: list[str] = []
            deleted_todo_ids: list[str] = []
            for todo in draft.todo_adds:
                created = self.todos.create(conn, task_id=workspace.task_id, command=CreateTodo(title=todo.title, category=todo.category, theme=todo.theme, priority=todo.priority, status=todo.status, status_reason=todo.status_reason, sources=todo.sources, actor=workspace.worker), now=now)
                todo_refs[todo.ref] = created.id
                created_todo_ids.append(created.id)
            for update_command in draft.todo_updates:
                target = todo_refs.get(update_command.todo_id, update_command.todo_id)
                values = update_command.model_dump(exclude_none=True, exclude={"todo_id", "source_adds"})
                if update_command.status == "completed":
                    values["completed_at"] = now
                elif update_command.status is not None:
                    values["completed_at"] = None
                values["updated_at"] = now
                updated = self.todos.update(conn, workspace.task_id, target, values)
                if updated is None:
                    raise NotFoundError(f"Todo not found: {target}")
                updated_todo_ids.append(target)
                self.todos.add_sources(conn, task_id=workspace.task_id, todo_id=target, sources=update_command.source_adds)
            for deletion in draft.todo_deletions:
                target = todo_refs.get(deletion.todo_id, deletion.todo_id)
                if target in todo_refs:
                    continue
                deleted = self.todos.update(conn, workspace.task_id, target, {"archived": True, "status": "obsolete", "status_reason": deletion.reason, "updated_at": now})
                if deleted is None:
                    raise NotFoundError(f"Todo not found: {target}")
                deleted_todo_ids.append(target)
            self._align_intent_counter(conn, workspace.task_id)
            created_intent_ids: list[str] = []
            intent_refs: dict[str, str] = {}
            for proposal in draft.intent_proposals:
                requested_execute_seconds = proposal.execute_seconds
                requested_conclude_seconds = proposal.conclude_seconds
                budget = resolve_explore_budget(
                    load_runtime_config(conn),
                    execute_seconds=requested_execute_seconds,
                    conclude_seconds=requested_conclude_seconds,
                )
                proposal = proposal.model_copy(
                    update={
                        "execute_seconds": budget.execute_seconds,
                        "conclude_seconds": budget.conclude_seconds,
                    }
                )
                if budget.used_legacy_values:
                    self._audit_in(
                        conn,
                        task=task,
                        plan_run_id=workspace.plan_run_id,
                        workspace_id=workspace.id,
                        worker=workspace.worker,
                        event_type="plan.intent_budget_normalized",
                        message="Plan Intent 的旧时间字段已由后端配置覆盖",
                        payload={
                            "legacy_execute_seconds": requested_execute_seconds,
                            "legacy_conclude_seconds": requested_conclude_seconds,
                            "execute_timeout_seconds": budget.execute_seconds,
                            "conclude_timeout_seconds": budget.conclude_seconds,
                            "legacy_values_ignored": True,
                        },
                    )
                todo_id = proposal.todo_id or todo_refs.get(proposal.todo_ref or "")
                if todo_id is None:
                    raise ConflictError(f"Intent references unknown Todo: {proposal.todo_ref}")
                self._validate_intent_proposal(conn, workspace, proposal)
                intent_id = allocate_id(conn, scope=workspace.task_id, kind="intent", prefix="i")
                plan_metadata = {
                    "plan_run_id": workspace.plan_run_id,
                    "plan_workspace_id": workspace.id,
                    "dedup_fingerprint": proposal.dedup_fingerprint or self.intent_fingerprint(proposal),
                    "from_facts": proposal.from_facts,
                    "source_refs": proposal.source_refs,
                    "why_now": proposal.why_now,
                    "endpoint": proposal.endpoint,
                    "test_vector": proposal.test_vector,
                    "expected_signal": proposal.expected_signal,
                    "stop_condition": proposal.stop_condition,
                }
                # Plan owns only its own planning worker pool.  The Explore
                # Intent it proposes must return to the normal task pool so
                # a Plan-only backend is never asked to execute probing work.
                values = {"id": intent_id, "task_id": workspace.task_id, "goal_cycle_id": self.repository.get_run(conn, workspace.task_id, workspace.plan_run_id).goal_cycle_id if self.repository.get_run(conn, workspace.task_id, workspace.plan_run_id) else None, "todo_id": todo_id, "kind": "explore", "description": proposal.description, "priority": proposal.priority, "status": "pending", "creator": workspace.worker, "worker": None, "worker_pool_json": json.dumps(task.worker_pool, ensure_ascii=False), "targets_json": json.dumps(proposal.targets, ensure_ascii=False), "dedup_fingerprint": proposal.dedup_fingerprint or self.intent_fingerprint(proposal), "plan_metadata_json": json.dumps(plan_metadata, ensure_ascii=False), "execute_timeout_seconds": proposal.execute_seconds, "conclude_timeout_seconds": proposal.conclude_seconds, "lease_id": None, "claimed_at": None, "last_heartbeat_at": None, "retry_count": 0, "infrastructure_retry_count": 0, "last_failure_kind": "", "failure_class": "", "activity_kind": "", "activity_summary": "", "activity_tool": "", "activity_started_at": None, "activity_updated_at": None, "next_retry_at": None, "to_fact_id": None, "human_intervention_required": False, "human_intervention_reason": "", "human_intervention_marked_at": None, "assessment_status": "open", "assessment_reason": "", "assessed_at": None, "created_at": now, "concluded_at": None, "archived": False}
                conn.execute(intents.insert().values(**values))
                for fact_id in proposal.from_facts:
                    if fact_id == "goal" or conn.execute(select(facts.c.id).where(facts.c.task_id == workspace.task_id, facts.c.id == fact_id, facts.c.archived.is_(False))).scalar_one_or_none() is None:
                        raise ConflictError(f"Intent source Fact not found: {fact_id}")
                    conn.execute(intent_sources.insert().values(task_id=workspace.task_id, intent_id=intent_id, fact_id=fact_id))
                created_intent_ids.append(intent_id)
                if proposal.ref:
                    if proposal.ref in intent_refs:
                        raise ConflictError(f"Duplicate Intent draft ref: {proposal.ref}")
                    intent_refs[proposal.ref] = intent_id
            # Red-team staging is applied after the Intent loop so
            # ``chain_intent_links`` can resolve staged Intent refs to the IDs
            # created above.  Chains are only persisted here, at commit time,
            # with the same objective/scope checks as the direct RedTeam API.
            created_chain_ids: list[str] = []
            chain_refs: dict[str, str] = {}
            if draft.chain_adds or draft.chain_updates or draft.chain_intent_links:
                if task.engagement_mode != "red_team":
                    raise ConflictError(
                        "RedTeam Chains are only available for red_team tasks"
                    )
                if self.red_team is None:
                    raise ConflictError("Red-team service is not configured")
                objective = task.red_team_objective
                for chain in draft.chain_adds:
                    initial_principal = chain.initial_principal
                    if initial_principal is None:
                        initial_principal = getattr(objective, "initial_principal", None)
                    if initial_principal is None:
                        raise ConflictError(
                            "Chain requires an initial principal and the task objective has none"
                        )
                    created_chain = self.red_team.create_chain_in(
                        conn,
                        workspace.task_id,
                        RedTeamChainCreate(
                            title=chain.title,
                            initial_principal=initial_principal,
                            target_capabilities=chain.target_capabilities,
                            target_assets=chain.target_assets,
                            source_fact_ids=chain.source_fact_ids,
                        ),
                        created_by=f"plan.{workspace.worker}",
                        goal_cycle_id=run.goal_cycle_id if run else None,
                        now=now,
                    )
                    chain_refs[chain.ref] = created_chain.id
                    created_chain_ids.append(created_chain.id)
                for chain_update in draft.chain_updates:
                    self.red_team.apply_chain_update_in(
                        conn,
                        workspace.task_id,
                        chain_update.chain_id,
                        status=chain_update.status,
                        title=chain_update.title,
                        actor=workspace.worker,
                        now=now,
                    )
                for chain_link in draft.chain_intent_links:
                    chain_id = chain_refs.get(chain_link.chain_ref) if chain_link.chain_ref else chain_link.chain_id
                    if chain_id is None:
                        raise ConflictError(
                            f"Chain link references unknown Chain: {chain_link.chain_ref or chain_link.chain_id}"
                        )
                    linked_intent_id = intent_refs.get(chain_link.intent_ref)
                    if linked_intent_id is None:
                        raise ConflictError(
                            f"Chain link references unknown Intent: {chain_link.intent_ref}"
                        )
                    self.red_team.link_intent_in(
                        conn,
                        workspace.task_id,
                        chain_id,
                        linked_intent_id,
                        actor=workspace.worker,
                        now=now,
                    )
            decision = draft.decision.model_dump(mode="json")
            baseline_snapshot = self._snapshot_from_run(run)
            post_snapshot = self._current_snapshot(conn, workspace.task_id)
            new_post_inputs = any(
                value not in set(baseline_snapshot.get(key, []))
                for key in PLAN_SNAPSHOT_KEYS
                if key != "open_explore_ids"
                for value in post_snapshot.get(key, [])
            )
            self.repository.update_decision(conn, task_id=workspace.task_id, run_id=workspace.plan_run_id, decision=decision, now=now)
            if run and not self.repository.complete(conn, task_id=workspace.task_id, run_id=workspace.plan_run_id, lease_id=lease_id, now=now):
                raise ConflictError("PlanRun completion lost a concurrent race")
            # Re-check shared queues at the commit boundary. A regular
            # Reason signal can arrive while Plan is running; its Fact is
            # intentionally excluded from this Plan's late-bound snapshot,
            # but queued Reason work must still keep the task out of waiting.
            regular_reason_work = conn.execute(
                select(reason_runs.c.id)
                .where(
                    reason_runs.c.task_id == workspace.task_id,
                    reason_runs.c.status.in_(("pending", "claimed", "running", "concluding")),
                    reason_runs.c.trigger != "plan",
                )
                .limit(1)
            ).scalar_one_or_none()
            open_explore_work = conn.execute(
                select(intents.c.id)
                .where(
                    intents.c.task_id == workspace.task_id,
                    intents.c.kind == "explore",
                    intents.c.status.in_(OPEN_EXPLORE_STATUSES),
                    intents.c.archived.is_(False),
                )
                .limit(1)
            ).scalar_one_or_none()
            queued_plan_work = bool(self.repository.pending_runs_for_task(conn, workspace.task_id))
            # Plan itself only schedules Explore.  A new input that arrived
            # while Plan was running must go through the normal Reason queue;
            # otherwise the task can jump directly to waiting and strand the
            # new Fact/assessment.  Explicit ``needs_reason`` is retained as
            # a worker decision, but it cannot override a freshly dispatched
            # Explore Intent.
            next_phase = (
                "explore"
                if created_intent_ids or open_explore_work
                else "reason"
                if (
                    decision["decision"] == "needs_reason"
                    or new_post_inputs
                    or regular_reason_work is not None
                )
                # ``continue`` is an explicit request for another global
                # review pass.  Keep the task in Plan so reconciliation can
                # enqueue the next FIFO PlanRun subject to the convergence
                # guard; treating it as waiting silently discards that
                # decision.
                else "plan"
                if decision["decision"] == "continue" or queued_plan_work
                else "waiting"
            )
            conn.execute(update(tasks).where(tasks.c.id == workspace.task_id).values(phase=next_phase, updated_at=now))
            committed = self.repository.mark_committed(conn, workspace_id, created_todo_ids=created_todo_ids, updated_todo_ids=updated_todo_ids, deleted_todo_ids=deleted_todo_ids, created_intent_ids=created_intent_ids, created_chain_ids=created_chain_ids, decision=decision, now=now)
            self._audit_in(
                conn,
                task=task,
                plan_run_id=workspace.plan_run_id,
                workspace_id=workspace.id,
                worker=workspace.worker,
                event_type="plan.workspace_committed",
                message="Plan Workspace 已提交",
                payload={
                    "decision": decision,
                    "created_todo_ids": created_todo_ids,
                    "updated_todo_ids": updated_todo_ids,
                    "deleted_todo_ids": deleted_todo_ids,
                    "created_intent_ids": created_intent_ids,
                    "created_chain_ids": created_chain_ids,
                    "new_post_inputs": new_post_inputs,
                    "phase": next_phase,
                },
            )
            result = PlanCommitResult(workspace=committed, created_todo_ids=created_todo_ids, updated_todo_ids=updated_todo_ids, deleted_todo_ids=deleted_todo_ids, created_intent_ids=created_intent_ids, created_chain_ids=created_chain_ids)
        if self.sandboxes is not None:
            try:
                self.sandboxes.cleanup_run_directory(
                    task_id=workspace.task_id,
                    owner_id=workspace.plan_run_id,
                    lease_id=lease_id,
                )
            except OSError:
                logger.exception(
                    "failed to clean committed run dir: %s/%s",
                    workspace.task_id,
                    workspace.plan_run_id,
                )
        return result

    @staticmethod
    def _align_todo_counter(conn, task_id: str) -> None:
        rows = conn.execute(select(todos.c.id).where(todos.c.task_id == task_id)).scalars().all()
        max_num = max((int(value[2:]) for value in rows if value.startswith("td") and value[2:].isdigit()), default=0)
        current = conn.execute(
            select(id_counters.c.next_value).where(
                id_counters.c.scope == task_id, id_counters.c.kind == "todo"
            )
        ).scalar_one_or_none()
        if current is not None and current < max_num + 1:
            conn.execute(
                update(id_counters)
                .where(id_counters.c.scope == task_id, id_counters.c.kind == "todo")
                .values(next_value=max_num + 1)
            )

    @staticmethod
    def _align_intent_counter(conn, task_id: str) -> None:
        rows = conn.execute(select(intents.c.id).where(intents.c.task_id == task_id)).scalars().all()
        max_num = max((int(value[1:]) for value in rows if value.startswith("i") and value[1:].isdigit()), default=0)
        current = conn.execute(
            select(id_counters.c.next_value).where(
                id_counters.c.scope == task_id, id_counters.c.kind == "intent"
            )
        ).scalar_one_or_none()
        if current is not None and current < max_num + 1:
            conn.execute(
                update(id_counters)
                .where(id_counters.c.scope == task_id, id_counters.c.kind == "intent")
                .values(next_value=max_num + 1)
            )
