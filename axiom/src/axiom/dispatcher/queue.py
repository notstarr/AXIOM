"""Queue-head readiness for the FIFO work queues.

Reason and Plan are strict FIFO: only the head of a Task's queue may be claimed,
so a later run can never overtake its predecessor.  That rule used to be
implemented by slicing the head *before* filtering out parked rows, which turned
one parked run into a permanent roadblock:

* the dispatcher selected the parked head, then skipped it as retry-blocked, so
  the healthy runs behind it were never even considered; and
* the claim endpoints validated FIFO against the unfiltered pending list, so a
  later run would have been rejected even if the dispatcher had offered it.

Both sides now share :func:`ready_runs` / :func:`fifo_head` so the predicate can
only change in one place.  A parked row keeps its position and its audit trail —
it is simply not the head while it is parked.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar


class QueuedRun(Protocol):
    """The two fields FIFO readiness depends on.

    Satisfied by both the dispatcher's ``*Snapshot`` models (``next_retry_at`` is
    a ``datetime``) and the module-level ``*Run`` models the claim endpoints read
    (``next_retry_at`` is an ISO string).  ``Any`` keeps both usable while the
    comparison stays inside :func:`ready_runs`.
    """

    id: str
    next_retry_at: Any


RunT = TypeVar("RunT", bound=QueuedRun)


def is_retry_blocked(work: object, infrastructure_retry_limit: int) -> bool:
    """Whether a pending row has exhausted its infrastructure retry budget.

    Such rows deliberately stay ``pending`` (so the audit trail and the reason
    string survive) but must not be offered as claim candidates.  They are
    re-armed by the automatic recovery pass, not by the scheduler.
    """

    return int(getattr(work, "infrastructure_retry_count", 0) or 0) >= infrastructure_retry_limit


def ready_runs(
    runs: Iterable[RunT],
    *,
    infrastructure_retry_limit: int,
    now: datetime | None = None,
) -> list[RunT]:
    """Return the queue in FIFO order with parked / backing-off rows removed.

    ``runs`` is expected to arrive already sorted by ``(created_at, id)``; order
    is preserved.  ``now`` is optional because the claim endpoints validate pure
    FIFO order, where a not-yet-due retry is still the head.
    """

    ready: list[RunT] = []
    for item in runs:
        if is_retry_blocked(item, infrastructure_retry_limit):
            continue
        if now is not None:
            next_retry_at = getattr(item, "next_retry_at", None)
            if next_retry_at is not None and next_retry_at > now:
                continue
        ready.append(item)
    return ready


def fifo_head(
    runs: Sequence[RunT],
    *,
    infrastructure_retry_limit: int,
    now: datetime | None = None,
) -> RunT | None:
    """The row a claim should accept as "the head of this Task's queue".

    Returns ``None`` when nothing is ready, which callers must treat as "this
    queue has no head right now" rather than "any row is acceptable" — that
    distinction is what keeps a parked queue from silently accepting a run that
    already has a ready predecessor.
    """

    ready = ready_runs(runs, infrastructure_retry_limit=infrastructure_retry_limit, now=now)
    return ready[0] if ready else None
