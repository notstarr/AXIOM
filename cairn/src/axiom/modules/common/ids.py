from __future__ import annotations

from sqlalchemy import Connection, case, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from axiom.infrastructure.database.metadata import id_counters


def allocate_readable_id(
    conn: Connection,
    *,
    scope: str,
    kind: str,
    prefix: str,
    width: int = 3,
) -> str:
    """Allocate a transaction-scoped, human-readable identifier.

    The dispatcher can register artifacts concurrently. A select-then-update
    counter lets two SQLite writers observe the same value and generate the
    same ID, which turns an otherwise successful Worker run into an
    ``IntegrityError``. SQLite's ``ON CONFLICT DO UPDATE`` makes the increment
    atomic while preserving the existing counter format.
    """

    value = allocate_sequence_value(conn, scope=scope, kind=kind, minimum=1)
    return f"{prefix}{value:0{width}d}"


def allocate_sequence_value(
    conn: Connection,
    *,
    scope: str,
    kind: str,
    minimum: int = 1,
) -> int:
    """Atomically allocate the next integer for a scoped sequence.

    ``minimum`` lets callers bootstrap a sequence from an existing table (for
    example, context revisions) without resetting a counter created by an
    earlier concurrent transaction.
    """
    minimum = max(1, int(minimum))
    if conn.dialect.name == "sqlite":
        next_value = id_counters.c.next_value
        candidate = case(
            (next_value > minimum, next_value),
            else_=minimum,
        )
        statement = (
            sqlite_insert(id_counters)
            .values(scope=scope, kind=kind, next_value=minimum + 1)
            .on_conflict_do_update(
                index_elements=[id_counters.c.scope, id_counters.c.kind],
                set_={"next_value": candidate + 1},
            )
            .returning(id_counters.c.next_value)
        )
        next_after_allocation = conn.execute(statement).scalar_one()
        return int(next_after_allocation) - 1

    current = conn.execute(
        select(id_counters.c.next_value).where(
            id_counters.c.scope == scope,
            id_counters.c.kind == kind,
        )
    ).scalar_one_or_none()
    if current is None:
        value = minimum
        conn.execute(id_counters.insert().values(scope=scope, kind=kind, next_value=value + 1))
    else:
        value = max(int(current), minimum)
        conn.execute(
            update(id_counters)
            .where(id_counters.c.scope == scope, id_counters.c.kind == kind)
            .values(next_value=value + 1)
        )
    return value
