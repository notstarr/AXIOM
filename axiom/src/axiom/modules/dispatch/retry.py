"""Retry budgeting shared by every released work item.

Intent, ReasonRun, PlanRun and VulnerabilityReviewRun all follow the same
rule, which used to be copy-pasted into each release path:

* ``execution`` failures become terminal once their configured limit is hit.
* ``transient`` (infrastructure) failures never become terminal.  At their
  limit the row stays ``pending`` with **no** retry timestamp, and claim
  validation pauses it until an operator or system recovery clears the
  condition.

That last state is a deliberate parking spot, so it must be announced rather
than silently skipped: :func:`retry_blocked_event` produces the audit record
operators and the UI need to see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from axiom.shared.clock import format_utc, parse_utc


# Cap the exponent so a long retry history can never overflow the multiply.
_MAX_BACKOFF_EXPONENT = 30


@dataclass(frozen=True)
class RetryOutcome:
    """What a released work item does next."""

    attempt: int
    limit: int
    terminal: bool
    retry_blocked: bool
    next_retry_at: str | None


def retry_outcome(config, *, attempt: int, failure_class: str, now: str) -> RetryOutcome:
    """Score one release attempt against the configured retry budget.

    ``attempt`` is the already-incremented counter for this failure class.
    """

    limit = (
        config.execution_retry_limit
        if failure_class == "execution"
        else config.infrastructure_retry_limit
    )
    terminal = failure_class == "execution" and attempt >= limit
    retry_blocked = failure_class == "transient" and attempt >= limit
    delay = min(
        config.retry_max_seconds,
        config.retry_base_seconds * (2 ** min(max(0, attempt - 1), _MAX_BACKOFF_EXPONENT)),
    )
    next_retry_at = (
        None
        if terminal or retry_blocked
        else format_utc(parse_utc(now) + timedelta(seconds=delay))
    )
    return RetryOutcome(
        attempt=attempt,
        limit=limit,
        terminal=terminal,
        retry_blocked=retry_blocked,
        next_retry_at=next_retry_at,
    )


def retry_blocked_message(phase: str, failure_kind: str) -> str:
    label = {
        "intent": "Intent",
        "reason": "ReasonRun",
        "plan": "PlanRun",
        "review": "VulnerabilityReviewRun",
    }.get(phase, phase)
    return (
        f"{label} 基础设施重试已达上限，已暂停自动重试，需人工恢复："
        f"{failure_kind or '未指定原因'}"
    )


def retry_blocked_event(
    *,
    phase: str,
    event_type: str,
    task_id: str,
    project_id: str | None,
    failure_kind: str,
    outcome: RetryOutcome,
    failed_worker: str | None,
    worker_pool: list[str],
    **ids: str | None,
):
    """Audit record announcing that an item is parked pending human recovery.

    The parked state is intentional, so it has to be visible: without this
    record the timeline simply stops after the last ``*.released`` and nothing
    distinguishes "waiting to retry" from "will never retry again".
    """

    from axiom.modules.audit.schemas import RecordAuditEvent

    payload: dict[str, object] = {
        "phase": phase,
        "failure_kind": failure_kind,
        "infrastructure_retry_count": outcome.attempt,
        "limit": outcome.limit,
        "failed_worker": failed_worker or "",
        "worker_pool": list(worker_pool),
    }
    payload.update({key: value for key, value in ids.items() if value})
    return RecordAuditEvent(
        project_id=project_id,
        task_id=task_id,
        worker=failed_worker or "",
        event_type=event_type,
        level="warning",
        message=retry_blocked_message(phase, failure_kind),
        payload=payload,
    )
