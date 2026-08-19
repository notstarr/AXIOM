"""Allow multiple pending ReasonRuns per Task while keeping at most one in-flight.

The single-task Reason queue feature creates additional ``pending`` runs
alongside one executing run.  The legacy partial unique index only allowed one
open (pending/claimed/running/concluding) run per task, so queued runs were
rejected with a unique-constraint violation.  Restrict the uniqueness to the
in-flight statuses; the number of queued ``pending`` runs is bounded by
``RuntimeConfig.reason_queue_limit`` at the application layer.
"""

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
            f'CREATE UNIQUE INDEX "uq_reason_runs_one_in_flight_per_task" '
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
