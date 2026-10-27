"""Worker-pool narrowing after a transient worker failure.

A work item inherits a worker pool at creation.  When an attempt dies from an
infrastructure fault, retrying with the *same* pool tends to re-select the same
worker — it is usually still the highest-priority eligible one, so the retry
budget burns out on a single broken backend and the item ends up parked.

Narrowing the pool is the cheap fix: drop the worker that just failed from this
item's pool so the next attempt lands on a different one.

The one sharp edge is that the dispatcher reads an **empty** pool as "any
capable worker" (``not work.worker_pool``), so persisting an empty list would
re-open the item to the very worker that just failed.  The narrowed result is
therefore never allowed to become empty.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from sqlalchemy import Connection, select, update

from axiom.modules.workers.tables import workers


def _clean(names: Iterable[str] | None) -> list[str]:
    return [str(name) for name in (names or []) if name]


def json_list(raw: Any) -> list[str]:
    """Read a ``worker_pool_json`` column value leniently."""

    if isinstance(raw, list):
        return _clean(raw)
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return _clean(parsed) if isinstance(parsed, list) else []


def narrow_worker_pool(
    pool: Sequence[str] | None,
    failed_worker: str | None,
    capable: Sequence[str] | None,
) -> list[str]:
    """Return the pool to persist after ``failed_worker`` lost this attempt.

    ``capable`` supplies the candidate set when the item has no explicit pool
    ("any worker"), so the failure can still be pinned to one backend.
    """

    current = _clean(pool)
    if not failed_worker:
        return current
    candidates = current or _clean(capable)
    if not candidates:
        return current
    narrowed = [name for name in candidates if name != failed_worker]
    # Never return []: the dispatcher treats an empty pool as "any worker", and
    # widening here would throw away the operator's restriction the moment the
    # only named worker had one bad attempt.
    #
    # A pool left naming only *unregistered* workers is harmless at this layer
    # because :func:`usable_pool` collapses it to "any worker" when the row is
    # read — that is the permanent case, and it is handled there.
    return narrowed or candidates


def usable_pool(pool: Sequence[str] | None, known: Iterable[str] | None) -> list[str]:
    """Return ``pool`` if it still names at least one registered worker.

    A pool is a *restriction*, and the dispatcher reads an empty pool as "any
    capable worker".  Two very different situations both look like "no worker in
    this list is available":

    * a known worker is cooling down or disabled — transient, so the item must
      keep waiting for it;
    * the list names only workers that are not registered at all (renamed,
      removed) — permanent, so waiting can never succeed.

    Only the second case is collapsed to ``[]``.  Left alone it is a silent,
    permanent stall: the item stays pending forever while every dispatch tick
    skips it, and nothing in the audit trail says why.
    """

    current = _clean(pool)
    if not current:
        return []
    known_names = set(_clean(known))
    if not known_names:
        # No worker is registered at all: that is a transient state (fresh
        # install, everything deleted), not evidence that the pool is stale.
        # Widening here would silently drop the operator's restriction for good.
        return current
    if not set(current) & known_names:
        return []
    return current


def registered_worker_names(conn: Connection) -> set[str]:
    """Every worker name in the registry, regardless of its current state.

    Deliberately not filtered by ``disabled`` or cooldown: :func:`usable_pool`
    must keep waiting for a worker that is merely temporarily unavailable.
    """

    return set(conn.execute(select(workers.c.name)).scalars())


def capable_worker_names(conn: Connection, kind: str) -> list[str]:
    """Eligible workers that advertise ``kind``."""

    names: list[str] = []
    rows = conn.execute(
        select(workers.c.name, workers.c.capabilities_json)
        .where(workers.c.disabled.is_(False))
        .order_by(workers.c.priority, workers.c.name)
    ).mappings()
    for row in rows:
        try:
            capabilities = json.loads(row["capabilities_json"] or "[]")
        except ValueError:
            capabilities = []
        if isinstance(capabilities, list) and kind in capabilities:
            names.append(row["name"])
    return names


def update_worker_pool(
    conn: Connection,
    *,
    table: Any,
    run_id: str,
    task_id: str,
    pool: Sequence[str],
    now: str,
) -> None:
    """Persist ``pool`` on one work item.

    Only called after :func:`narrow_worker_pool`, which guarantees a non-empty
    list, so this never has to re-check that invariant.

    ``intents`` is the one lifecycle table without an ``updated_at`` column, so
    that value is only written when the table actually has it.
    """

    values: dict[str, Any] = {"worker_pool_json": json.dumps(list(pool), ensure_ascii=False)}
    if "updated_at" in table.c:
        values["updated_at"] = now
    conn.execute(
        update(table)
        .where(table.c.id == run_id, table.c.task_id == task_id)
        .values(**values)
    )


def narrow_pool_after_failure(
    conn: Connection,
    *,
    table: Any,
    run_id: str,
    task_id: str,
    pool_json: Any,
    failed_worker: str | None,
    kind: str,
    now: str,
) -> list[str]:
    """Narrow one work item's pool after its worker failed; return the pool.

    Called only for infrastructure (transient) failures.  Persists the narrowed
    pool when it actually changed, so a single broken backend cannot absorb the
    whole retry budget.

    The capable set is read unconditionally.  Fetching it only when the item had
    no pool of its own left an item that *did* have one narrowing against an
    empty set of alternatives, so it kept re-selecting the backend that had just
    failed.
    """

    current = json_list(pool_json)
    capable = capable_worker_names(conn, kind)
    narrowed = narrow_worker_pool(current, failed_worker, capable)
    if narrowed != current:
        update_worker_pool(
            conn, table=table, run_id=run_id, task_id=task_id, pool=narrowed, now=now
        )
    return narrowed
