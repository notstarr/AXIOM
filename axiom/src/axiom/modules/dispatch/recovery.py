"""Automatic recovery of parked work items.

A work item whose infrastructure retries are exhausted stays ``pending`` with no
``next_retry_at``.  That is deliberate — the audit trail and the last failure
reason survive — but it used to be permanent: nothing in AXIOM re-armed it, so a
provider that was merely slow for twenty minutes could park a Task for the rest
of its life and only an operator with a shell could restart it.

This module closes that gap.  A parked row is re-armed once it has been quiet for
``retry_recovery_quiet_seconds``, which is long enough that the fault has either
cleared or is real, and short enough that a transient upstream outage costs a
few minutes instead of a whole night.

Two guards keep recovery from becoming an infinite retry loop:

* ``recovery_round`` is incremented on every re-arm, and recovery stops once it
  reaches ``retry_recovery_max_rounds``.  A misconfigured backend (revoked key,
  wrong base URL) therefore costs bounded attempts, not endless ones.
* The exhausted state is announced once, when recovery gives up, so the Task
  visibly needs a human instead of quietly looping.

The worker pool is deliberately left alone: it records which backend failed, and
erasing it would destroy the evidence the breaker uses to avoid that backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import Connection, select, update

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import intents
from axiom.modules.plan.tables import plan_runs
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.shared.clock import format_utc, parse_utc


# The four queues that park.  The label is what the audit record calls the kind,
# and ``event_prefix`` is its existing event namespace.
_PARKED_QUEUES = (
    ("intent", intents, "intent"),
    ("reason", reason_runs, "reason_run"),
    ("plan", plan_runs, "plan_run"),
    ("review", vulnerability_review_runs, "vulnerability_review"),
)


@dataclass
class RecoveredItem:
    phase: str
    task_id: str
    run_id: str
    previous_retry_count: int
    recovery_round: int


@dataclass
class RecoveryOutcome:
    recovered: list[RecoveredItem] = field(default_factory=list)
    exhausted: list[RecoveredItem] = field(default_factory=list)
    skipped: int = 0

    def empty(self) -> bool:
        return not self.recovered and not self.exhausted and self.skipped == 0


def _reset_values(table, *, now: str, recovery_round: int, config) -> dict:
    """The exact state a re-armed row must have.

    Mirrors the operator-facing reset endpoints so an automatically recovered row
    is indistinguishable from a hand-reset one — one code path, one meaning.
    """

    values: dict = {
        "infrastructure_retry_count": 0,
        "next_retry_at": None,
        "retry_blocked_at": None,
        "recovery_round": recovery_round,
        "last_failure_kind": "",
        "failure_class": "",
        "activity_kind": "",
        "activity_summary": "",
        "activity_tool": "",
        "activity_started_at": None,
        "activity_updated_at": now,
        "updated_at": now,
    }
    # Execution retries are a separate budget; clearing them would let a row that
    # genuinely fails every time start over, which is not what recovery means.
    if "concluded_at" in table.c:
        values["concluded_at"] = None
    return values


def recover_parked(
    conn: Connection,
    *,
    audit: AuditService,
    config,
    now: str,
) -> RecoveryOutcome:
    """Re-arm parked work items that have been quiet long enough."""

    outcome = RecoveryOutcome()
    if not bool(getattr(config, "retry_recovery_enabled", True)):
        return outcome

    quiet_seconds = max(1, int(getattr(config, "retry_recovery_quiet_seconds", 900)))
    max_rounds = max(0, int(getattr(config, "retry_recovery_max_rounds", 5)))
    limit = max(1, int(getattr(config, "infrastructure_retry_limit", 3)))
    cutoff = format_utc(parse_utc(now) - timedelta(seconds=quiet_seconds))

    # Only "running, unarchived" tasks: a paused or archived Task must keep its
    # parked rows exactly as they are, because re-arming them would make work
    # resumable that the operator deliberately stopped.
    live_tasks = {
        row["id"]: row["project_id"]
        for row in conn.execute(
            select(tasks.c.id, tasks.c.project_id).where(
                tasks.c.status == "running",
                tasks.c.archived.is_(False),
            )
        ).mappings()
    }
    if not live_tasks:
        return outcome

    for phase, table, event_prefix in _PARKED_QUEUES:
        rows = conn.execute(
            select(table).where(
                table.c.status == "pending",
                table.c.infrastructure_retry_count >= limit,
                table.c.next_retry_at.is_(None),
                table.c.retry_blocked_at.is_not(None),
                table.c.retry_blocked_at <= cutoff,
            )
        ).mappings().all()
        for row in rows:
            task_id = row["task_id"]
            if task_id not in live_tasks:
                outcome.skipped += 1
                continue
            run_id = row["id"]
            previous = int(row["infrastructure_retry_count"] or 0)
            rounds = int(row["recovery_round"] or 0)
            if rounds >= max_rounds:
                # Announce once, then leave it parked.  ``recovery_round`` stops
                # at max_rounds so the "exhausted" flag is written exactly once.
                if rounds == max_rounds:
                    _audit(
                        conn,
                        audit,
                        event_prefix=event_prefix,
                        event_suffix="recovery_exhausted",
                        level="error",
                        task_id=task_id,
                        project_id=live_tasks[task_id],
                        run_id=run_id,
                        message=(
                            f"{_label(phase)} 自动恢复已达上限（{max_rounds} 次），"
                            "已停止自动重试，需人工介入"
                        ),
                        payload={
                            "phase": phase,
                            "infrastructure_retry_count": previous,
                            "recovery_round": rounds,
                            "last_failure_kind": row["last_failure_kind"] or "",
                            "max_rounds": max_rounds,
                        },
                    )
                    conn.execute(
                        update(table)
                        .where(table.c.id == run_id, table.c.task_id == task_id)
                        .values(recovery_round=rounds + 1)
                    )
                    outcome.exhausted.append(
                        RecoveredItem(phase, task_id, run_id, previous, rounds)
                    )
                else:
                    outcome.skipped += 1
                continue

            updated = conn.execute(
                update(table)
                .where(
                    table.c.id == run_id,
                    table.c.task_id == task_id,
                    table.c.status == "pending",
                    table.c.infrastructure_retry_count >= limit,
                )
                .values(
                    **_reset_values(
                        table, now=now, recovery_round=rounds + 1, config=config
                    )
                )
            )
            if not updated.rowcount:
                # Someone (an operator or another recovery pass) moved it first.
                outcome.skipped += 1
                continue
            _audit(
                conn,
                audit,
                event_prefix=event_prefix,
                event_suffix="auto_recovered",
                level="warning",
                task_id=task_id,
                project_id=live_tasks[task_id],
                run_id=run_id,
                message=(
                    f"{_label(phase)} 已自动恢复（第 {rounds + 1} 次），重新排队领取"
                ),
                payload={
                    "phase": phase,
                    "previous_infrastructure_retry_count": previous,
                    "recovery_round": rounds + 1,
                    "max_rounds": max_rounds,
                    "last_failure_kind": row["last_failure_kind"] or "",
                    "quiet_seconds": quiet_seconds,
                    "worker_pool": _pool(row["worker_pool_json"]),
                },
            )
            outcome.recovered.append(
                RecoveredItem(phase, task_id, run_id, previous, rounds + 1)
            )
    return outcome


def _pool(raw) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(name) for name in parsed] if isinstance(parsed, list) else []


def _label(phase: str) -> str:
    return {
        "intent": "Intent",
        "reason": "ReasonRun",
        "plan": "PlanRun",
        "review": "ReviewRun",
    }.get(phase, phase)


def _audit(
    conn: Connection,
    audit: AuditService,
    *,
    event_prefix: str,
    event_suffix: str,
    level: str,
    task_id: str,
    project_id: str | None,
    run_id: str,
    message: str,
    payload: dict,
) -> None:
    kwargs: dict = {}
    if event_prefix == "intent":
        kwargs["intent_id"] = run_id
    elif event_prefix == "reason_run":
        kwargs["reason_run_id"] = run_id
    elif event_prefix == "plan_run":
        kwargs["plan_run_id"] = run_id
    else:
        kwargs["review_run_id"] = run_id
    audit.record_in(
        conn,
        RecordAuditEvent(
            project_id=project_id,
            task_id=task_id,
            worker="",
            event_type=f"{event_prefix}.{event_suffix}",
            level=level,
            message=message,
            payload=payload,
            **kwargs,
        ),
    )
