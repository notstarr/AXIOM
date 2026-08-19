"""Allow a FIFO Reason queue while keeping one in-flight run per task."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260819_pg0005"
down_revision = "20260814_pg0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            f'DROP INDEX IF EXISTS "{AXIOM_SCHEMA}"."uq_reason_runs_one_open_per_task"'
        )
    )
    op.execute(
        sa.text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_reason_runs_one_in_flight_per_task" '
            f'ON "{AXIOM_SCHEMA}"."reason_runs" (task_id) '
            f"WHERE status IN ('claimed','running','concluding')"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f'DROP INDEX IF EXISTS "{AXIOM_SCHEMA}"."uq_reason_runs_one_in_flight_per_task"'
        )
    )
    op.execute(
        sa.text(
            f'CREATE UNIQUE INDEX "uq_reason_runs_one_open_per_task" '
            f'ON "{AXIOM_SCHEMA}"."reason_runs" (task_id) '
            f"WHERE status IN ('pending','claimed','running','concluding')"
        )
    )
