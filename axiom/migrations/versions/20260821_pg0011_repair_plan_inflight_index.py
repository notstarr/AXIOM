"""Repair the Plan in-flight partial unique index on PostgreSQL."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260821_pg0011"
down_revision = "20260821_pg0010"
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('claimed','running','concluding')"


def _recreate() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f'DROP INDEX IF EXISTS "{AXIOM_SCHEMA}"."uq_plan_runs_one_in_flight_per_task"'
        )
    )
    bind.execute(
        sa.text(
            f'CREATE UNIQUE INDEX "uq_plan_runs_one_in_flight_per_task" '
            f'ON "{AXIOM_SCHEMA}"."plan_runs" (task_id) WHERE {_ACTIVE}'
        )
    )


def upgrade() -> None:
    _recreate()


def downgrade() -> None:
    # 0038/pg0007 intended a partial index as well; preserve that invariant
    # while allowing a clean Alembic step-down.
    _recreate()
