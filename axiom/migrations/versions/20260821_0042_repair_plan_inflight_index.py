"""Make the Plan in-flight uniqueness predicate portable and idempotent."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260821_0042"
down_revision = "20260821_0041"
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('claimed','running','concluding')"


def _recreate() -> None:
    bind = op.get_bind()
    # DROP INDEX IF EXISTS works on both SQLite and PostgreSQL.  The schema is
    # implicit on the generic branch; PostgreSQL's dedicated branch performs
    # the schema-qualified equivalent below.
    bind.exec_driver_sql("DROP INDEX IF EXISTS uq_plan_runs_one_in_flight_per_task")
    kwargs = {"sqlite_where": sa.text(_ACTIVE)}
    if bind.dialect.name == "postgresql":
        kwargs["postgresql_where"] = sa.text(_ACTIVE)
    op.create_index(
        "uq_plan_runs_one_in_flight_per_task",
        "plan_runs",
        ["task_id"],
        unique=True,
        **kwargs,
    )


def upgrade() -> None:
    _recreate()


def downgrade() -> None:
    # The intended contract of 0038 was already a partial unique index.  Keep
    # that contract when stepping back instead of reintroducing the buggy
    # full-table uniqueness behavior.
    _recreate()
