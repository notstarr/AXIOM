"""Task-level bulk reset of the retry budget for every queue of one Task.

An operator staring at a stalled Task sees *one* problem ("nothing is moving"),
not four ("this Intent is parked, that ReasonRun is parked, ...").  The
per-item escape hatches exist — ``reset-retries`` on an Intent and on a
ReasonRun — but they make the operator walk the graph one node at a time, and
the queue they cannot reach (Plan, Review) has no button at all.

This module is the Task-level counterpart: one call clears the retry budget of
*every* work item of a Task across all four dispatch queues, so the workers
can pick the work up again immediately instead of waiting out the quiet
period.

What "budget" means here is the retry budget, not the runtime budget: the
latter (``execute_seconds`` / ``conclude_seconds``) is a time allowance the
worker is given, and zeroing it would make every item fail instantly.  The
retry budget is the allowance of *infrastructure* failures — the counter that
runs out and parks an item so no dispatch tick will ever offer it again.

The reset deliberately touches only pending work.  A running item owns a live
lease; clearing its counters would hand two workers the same item.  A
completed or cancelled item has no budget left to reset.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, Table, update

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import intents
from axiom.modules.plan.tables import plan_runs
from axiom.modules.reason.tables import reason_runs
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs


@dataclass(frozen=True, slots=True)
class ResetQueue:
    """One dispatch queue of a Task and the label used in the audit trail."""

    phase: str
    table: Table
    label: str


QUEUES: tuple[ResetQueue, ...] = (
    ResetQueue("intent", intents, "Intent"),
    ResetQueue("reason", reason_runs, "ReasonRun"),
    ResetQueue("plan", plan_runs, "PlanRun"),
    ResetQueue("review", vulnerability_review_runs, "ReviewRun"),
)


@dataclass(frozen=True, slots=True)
class ResetOutcome:
    """How many work items each queue contributed."""

    reset_by_phase: dict[str, int]
    total: int

    def as_dict(self) -> dict[str, object]:
        return {"reset_by_phase": dict(self.reset_by_phase), "total": self.total}


def _has_retry_debris(row) -> bool:
    """Whether a row carries anything worth clearing.

    A row with a non-zero counter, a scheduled retry, a recorded failure or a
    parked timestamp all count.  Rows that never failed are left alone so the
    audit trail records what actually changed.
    """

    return bool(
        int(row["infrastructure_retry_count"] or 0) > 0
        or int(row["retry_count"] or 0) > 0
        or row["failure_class"]
        or row["last_failure_kind"]
        or row["next_retry_at"] is not None
        or row["retry_blocked_at"] is not None
    )


def reset_task_retry_budget(
    conn: Connection,
    *,
    task_id: str,
    audit: AuditService | None = None,
    project_id: str | None = None,
) -> ResetOutcome:
    """Clear the retry budget of every pending work item of one Task.

    Returns the number of rows actually touched per queue.  Rows that were
    never parked and never failed are not counted: "reset" should report the
    work it unblocked, not the work it merely looked at.
    """

    reset_by_phase: dict[str, int] = {}
    for queue in QUEUES:
        table = queue.table
        rows = conn.execute(
            table.select().where(
                table.c.task_id == task_id,
                table.c.status == "pending",
            )
        ).mappings().all()
        changed = 0
        for row in rows:
            if not _has_retry_debris(row):
                continue
            conn.execute(
                update(table)
                .where(table.c.task_id == task_id, table.c.id == row["id"])
                .values(
                    retry_count=0,
                    infrastructure_retry_count=0,
                    next_retry_at=None,
                    retry_blocked_at=None,
                    recovery_round=0,
                    last_failure_kind="",
                    failure_class="",
                )
            )
            changed += 1
        reset_by_phase[queue.phase] = changed
        if changed and audit is not None:
            audit.record_in(
                conn,
                RecordAuditEvent(
                    event_type=f"{queue.phase}.retry_budget_reset",
                    task_id=task_id,
                    project_id=project_id,
                    level="warning",
                    message=(
                        f"已重置 {queue.label} 的重试预算：{changed} 条，"
                        "Worker 可立即重新认领"
                    ),
                    payload={"phase": queue.phase, "reset_count": changed},
                ),
            )
    return ResetOutcome(
        reset_by_phase=reset_by_phase,
        total=sum(reset_by_phase.values()),
    )
