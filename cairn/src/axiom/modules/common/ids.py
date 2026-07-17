from __future__ import annotations

from sqlalchemy import Connection, select, update

from axiom.infrastructure.database.metadata import id_counters


def allocate_readable_id(
    conn: Connection,
    *,
    scope: str,
    kind: str,
    prefix: str,
    width: int = 3,
) -> str:
    """Allocate a transaction-scoped, human-readable identifier."""

    current = conn.execute(
        select(id_counters.c.next_value).where(
            id_counters.c.scope == scope,
            id_counters.c.kind == kind,
        )
    ).scalar_one_or_none()
    if current is None:
        value = 1
        conn.execute(
            id_counters.insert().values(scope=scope, kind=kind, next_value=2)
        )
    else:
        value = int(current)
        conn.execute(
            update(id_counters)
            .where(id_counters.c.scope == scope, id_counters.c.kind == kind)
            .values(next_value=value + 1)
        )
    return f"{prefix}{value:0{width}d}"
