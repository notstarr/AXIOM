from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Column, Connection, Integer, PrimaryKeyConstraint, String, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert

from axiom.shared.database import metadata


id_counters = Table(
    "id_counters",
    metadata,
    Column("scope", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("next_value", Integer, nullable=False),
    PrimaryKeyConstraint("scope", "kind"),
)


def initialize_counter(
    conn: Connection,
    *,
    scope: str,
    kind: str,
    next_value: int = 1,
) -> None:
    conn.execute(
        pg_insert(id_counters)
        .values(scope=scope, kind=kind, next_value=next_value)
        .on_conflict_do_nothing(index_elements=["scope", "kind"])
    )


def allocate_id(
    conn: Connection,
    *,
    scope: str,
    kind: str,
    prefix: str,
    width: int = 3,
) -> str:
    current = allocate_sequence(conn, scope=scope, kind=kind)
    return f"{prefix}{current:0{width}d}"


def allocate_sequence(
    conn: Connection,
    *,
    scope: str,
    kind: str,
) -> int:
    statement = (
        pg_insert(id_counters)
        .values(scope=scope, kind=kind, next_value=2)
        .on_conflict_do_update(
            index_elements=["scope", "kind"],
            set_={"next_value": id_counters.c.next_value + 1},
        )
        .returning(id_counters.c.next_value)
    )
    next_value = conn.execute(statement).scalar_one()
    return next_value - 1


def new_lease_id() -> str:
    """Create an opaque ownership token for one Intent claim attempt."""

    return f"lease_{uuid4().hex}"
