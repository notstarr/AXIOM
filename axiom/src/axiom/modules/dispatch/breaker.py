"""Per-Worker circuit breaker.

``workers.cooldown_until`` already existed, but only as a bare timestamp written
with one fixed delay (``worker_failure_cooldown_seconds``) and never reset on
success.  Two consequences:

* a backend that keeps failing is re-tried at the same gentle interval forever,
  so a broken endpoint keeps absorbing claims; and
* a backend that recovers looks identical to one that was never tried again, so
  there was no way to decay the penalty.

This module adds the missing history — consecutive failures, trip count, and when
the last failure happened — and derives the cooldown from it.  The design point is
that there is **no success callback**: a success is inferred from the absence of
recent failures.  A backend that has been quiet for ``worker_breaker_decay_seconds``
has its ladder reset, so nothing has to remember to call "it worked".

Escalation is deliberately capped: after a few trips the cooldown stops growing,
because the goal is to stop hammering a broken backend, not to exile it forever.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Connection, select, update

from axiom.modules.workers.tables import workers
from axiom.shared.clock import format_utc, parse_utc


def cooldown_for_trip(trip: int, config) -> int:
    """Seconds to cool a backend that just tripped for the ``trip``-th time.

    The first trip uses the historical ``worker_failure_cooldown_seconds`` so an
    existing deployment behaves as before until a backend actually misbehaves
    repeatedly; each later trip multiplies by ``worker_failure_cooling_factor``
    up to ``worker_failure_cooldown_max_seconds``.
    """

    base = max(1, int(getattr(config, "worker_failure_cooldown_seconds", 30)))
    factor = max(1, int(getattr(config, "worker_failure_cooling_factor", 5)))
    ceiling = max(base, int(getattr(config, "worker_failure_cooldown_max_seconds", 1800)))
    if trip <= 1:
        return min(base, ceiling)
    # Cap the exponent so a long trip history cannot overflow the multiply.
    exponent = min(trip - 1, 16)
    return min(base * (factor ** exponent), ceiling)


def _decayed(failures: int, trips: int, last_failure_at: str | None, now: str, config) -> tuple[int, int]:
    """Apply the quiet-period decay to a breaker's counters."""

    if not last_failure_at:
        return failures, trips
    decay = max(1, int(getattr(config, "worker_breaker_decay_seconds", 3600)))
    try:
        quiet = (parse_utc(now) - parse_utc(last_failure_at)).total_seconds()
    except (TypeError, ValueError):
        return failures, trips
    if quiet < decay:
        return failures, trips
    # Quiet long enough: the backend deserves a clean slate, including its
    # escalation history — otherwise one bad afternoon would follow it forever.
    return 0, 0


def trip(
    conn: Connection,
    *,
    worker_name: str,
    failure_kind: str,
    now: str,
    config,
) -> None:
    """Record one transient failure for a Worker and update its cooldown.

    Called from every release path that classified a failure as ``transient``.
    Until ``worker_failure_trip_after`` consecutive failures accumulate, the
    backend only gets the base cooldown — a single hiccup should not escalate.
    """

    if not worker_name:
        return
    row = conn.execute(
        select(workers).where(workers.c.name == worker_name)
    ).mappings().one_or_none()
    if row is None:
        return
    failures, trips = _decayed(
        int(row["transient_failure_count"] or 0),
        int(row["trip_count"] or 0),
        row["last_failure_at"],
        now,
        config,
    )
    failures += 1
    trip_after = max(1, int(getattr(config, "worker_failure_trip_after", 2)))
    if failures >= trip_after:
        trips += 1
        failures = 0
    seconds = cooldown_for_trip(trips if trips else 1, config)
    proposed = parse_utc(now) + timedelta(seconds=seconds)
    # Never shorten a cooldown that is already in force: an operator-chosen
    # window is a decision, and a run that was in flight when it was set must not
    # be able to cut it short.  Escalation only ever extends.
    existing = row["cooldown_until"]
    if existing:
        try:
            proposed = max(proposed, parse_utc(existing))
        except (TypeError, ValueError):
            pass
    cooled_to = format_utc(proposed)
    conn.execute(
        update(workers)
        .where(workers.c.name == worker_name)
        .values(
            transient_failure_count=failures,
            trip_count=trips,
            last_failure_at=now,
            last_failure_kind=str(failure_kind or "")[:200],
            cooldown_until=cooled_to,
            updated_at=now,
        )
    )


def reset(conn: Connection, *, worker_name: str, now: str) -> bool:
    """Clear a Worker's breaker state (the "立即恢复" action).

    Deliberately does not probe the backend: an operator asking for recovery has
    a reason, and a probe would either duplicate the next real claim or make the
    button slow and flaky.  The next failure re-trips it immediately.
    """

    result = conn.execute(
        update(workers)
        .where(workers.c.name == worker_name)
        .values(
            transient_failure_count=0,
            trip_count=0,
            cooldown_until=None,
            last_failure_kind="",
            updated_at=now,
        )
    )
    return bool(result.rowcount)
