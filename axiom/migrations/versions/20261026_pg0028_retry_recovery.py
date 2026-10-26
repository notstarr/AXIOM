"""Durable retry-parking markers and worker circuit-breaker state.

Two things are added here, both in service of automatic recovery:

``retry_blocked_at`` / ``recovery_round`` on the four lifecycle tables.  A work
item whose infrastructure retries are exhausted stays ``pending`` with no
``next_retry_at`` so the audit trail survives — but that state was previously
only *inferable* (count at limit AND no retry timestamp), which cannot express
"parked since when" or "how many times has recovery already re-armed this".
Without those two facts a recovery pass would either never fire or fire forever.
``retry_blocked_at`` is written where the item parks; ``recovery_round`` counts
how many times automation has already reset it, so a permanently broken
configuration stops retrying instead of looping.

``transient_failure_count`` / ``trip_count`` / ``last_failure_at`` /
``last_failure_kind`` on ``workers``.  The existing ``cooldown_until`` only holds
a timestamp with no history, so the breaker had no way to escalate a repeatedly
failing backend, and no way to reset itself once the backend went quiet.

Historical parked rows are intentionally NOT backfilled: a row that parked before
this migration has no reliable parking time, and inventing one would let the
first recovery pass re-arm an unknown backlog.  Operators release those with the
existing reset endpoints (or one pass of ``POST /dispatch/recover-parked`` after
setting ``retry_blocked_at``) — see the delivery notes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261026_pg0028"
down_revision = "20261026_pg0027"
branch_labels = None
depends_on = None


# Every table that parks a released work item, with the id column the recovery
# pass matches on.  Keeping them in one tuple makes it impossible to add the
# columns to only some of the queues.
_PARKED_TABLES = (
    "intents",
    "reason_runs",
    "plan_runs",
    "vulnerability_review_runs",
)

_WORKER_COLUMNS = (
    ("transient_failure_count", sa.Column(
        "transient_failure_count",
        sa.Integer(),
        nullable=False,
        server_default="0",
    )),
    ("trip_count", sa.Column(
        "trip_count",
        sa.Integer(),
        nullable=False,
        server_default="0",
    )),
    ("last_failure_at", sa.Column(
        "last_failure_at",
        sa.DateTime(timezone=True),
        nullable=True,
    )),
    ("last_failure_kind", sa.Column(
        "last_failure_kind",
        sa.Text(),
        nullable=False,
        server_default="",
    )),
)

_PARKED_COLUMNS = (
    ("retry_blocked_at", sa.Column(
        "retry_blocked_at",
        sa.DateTime(timezone=True),
        nullable=True,
    )),
    ("recovery_round", sa.Column(
        "recovery_round",
        sa.Integer(),
        nullable=False,
        server_default="0",
    )),
)


def _columns(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table, schema=AXIOM_SCHEMA)
    }


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=AXIOM_SCHEMA)


def upgrade() -> None:
    if _has_table("workers"):
        existing = _columns("workers")
        for name, column in _WORKER_COLUMNS:
            if name not in existing:
                op.add_column("workers", column, schema=AXIOM_SCHEMA)

    for table in _PARKED_TABLES:
        if not _has_table(table):
            continue
        existing = _columns(table)
        for name, column in _PARKED_COLUMNS:
            if name not in existing:
                op.add_column(table, column, schema=AXIOM_SCHEMA)
        # The recovery pass scans "parked, quiet since X" across all tasks, so the
        # index is on the parking timestamp rather than on a task id.
        index_name = f"ix_{table}_retry_blocked_at"
        if "retry_blocked_at" in _columns(table):
            bound = op.get_bind()
            indexes = {
                item["name"]
                for item in sa.inspect(bound).get_indexes(table, schema=AXIOM_SCHEMA)
            }
            if index_name not in indexes:
                op.create_index(
                    index_name,
                    table,
                    ["retry_blocked_at"],
                    schema=AXIOM_SCHEMA,
                    postgresql_where=sa.text("retry_blocked_at IS NOT NULL"),
                )


def downgrade() -> None:
    for table in _PARKED_TABLES:
        if not _has_table(table):
            continue
        bound = op.get_bind()
        indexes = {
            item["name"]
            for item in sa.inspect(bound).get_indexes(table, schema=AXIOM_SCHEMA)
        }
        index_name = f"ix_{table}_retry_blocked_at"
        if index_name in indexes:
            op.drop_index(index_name, table_name=table, schema=AXIOM_SCHEMA)
        existing = _columns(table)
        for name, _column in reversed(_PARKED_COLUMNS):
            if name in existing:
                op.drop_column(table, name, schema=AXIOM_SCHEMA)

    if _has_table("workers"):
        existing = _columns("workers")
        for name, _column in reversed(_WORKER_COLUMNS):
            if name in existing:
                op.drop_column("workers", name, schema=AXIOM_SCHEMA)
