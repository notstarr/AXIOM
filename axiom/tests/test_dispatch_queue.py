"""FIFO queue-head readiness (``axiom.dispatcher.queue``).

The regression these cover: a run parked at its infrastructure retry limit used
to hold the head of its queue.  The dispatcher selected it and then skipped it, so
nothing behind it was ever offered; the claim endpoints validated FIFO against the
unfiltered list, so they would have rejected a later run anyway.  One parked row
stopped the whole Task.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from axiom.dispatcher.queue import fifo_head, is_retry_blocked, ready_runs
from axiom.dispatcher.schemas import PlanRunSnapshot, ReasonRunSnapshot


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _reason(number: int, *, retries: int = 0, next_retry_at: datetime | None = None):
    return ReasonRunSnapshot(
        task_id="t_001",
        id=f"rr{number:06d}",
        status="pending",
        infrastructure_retry_count=retries,
        next_retry_at=next_retry_at,
        created_at=NOW + timedelta(seconds=number),
    )


def _plan(number: int, *, retries: int = 0, next_retry_at: datetime | None = None):
    return PlanRunSnapshot(
        task_id="t_001",
        id=f"pr{number:06d}",
        status="pending",
        infrastructure_retry_count=retries,
        next_retry_at=next_retry_at,
        created_at=NOW + timedelta(seconds=number),
    )


def test_parked_head_is_not_the_head_but_keeps_its_place() -> None:
    queue = [_reason(449, retries=3), _reason(500)]

    assert ready_runs(queue, infrastructure_retry_limit=3) == [queue[1]]
    assert fifo_head(queue, infrastructure_retry_limit=3) is queue[1]


def test_parked_rows_only_do_not_form_a_head() -> None:
    queue = [_reason(449, retries=3), _reason(450, retries=4)]

    assert fifo_head(queue, infrastructure_retry_limit=3) is None


def test_head_order_is_fifo_among_ready_rows() -> None:
    queue = [_reason(1, retries=3), _reason(2), _reason(3), _reason(4)]

    assert fifo_head(queue, infrastructure_retry_limit=3) is queue[1]


def test_backing_off_head_is_skipped_only_when_now_is_supplied() -> None:
    """The claim path validates pure FIFO order, so it passes ``now=None``.

    A retry that is scheduled but not yet due still owns the head: letting a
    later run overtake it would reorder two runs that both intend to execute.
    The dispatcher, which decides whether to *claim* right now, does filter it.
    """

    queue = [_reason(1, next_retry_at=NOW + timedelta(minutes=5)), _reason(2)]

    assert fifo_head(queue, infrastructure_retry_limit=3) is queue[0]
    assert fifo_head(queue, infrastructure_retry_limit=3, now=NOW) is queue[1]


def test_plan_queue_uses_the_same_predicate() -> None:
    queue = [_plan(1, retries=3), _plan(2)]

    assert fifo_head(queue, infrastructure_retry_limit=3) is queue[1]


def test_limit_boundary_blocks_at_the_limit_not_above_it() -> None:
    assert not is_retry_blocked(_reason(1, retries=2), 3)
    assert is_retry_blocked(_reason(1, retries=3), 3)
    assert is_retry_blocked(_reason(1, retries=4), 3)


def test_missing_retry_fields_are_treated_as_zero() -> None:
    """Snapshots from an older control plane may omit the counter entirely."""

    class Bare:
        id = "x"
        next_retry_at = None

    assert not is_retry_blocked(Bare(), 3)
    assert fifo_head([Bare()], infrastructure_retry_limit=3) is not None
