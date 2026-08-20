"""Allow Tasks to enter the Plan panorama phase."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260820_pg0006"
down_revision = "20260819_pg0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."tasks" '
            'DROP CONSTRAINT IF EXISTS "ck_tasks_phase"'
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."tasks" '
            "ADD CONSTRAINT \"ck_tasks_phase\" CHECK "
            "(phase IN ('bootstrap','reason','explore','plan','waiting','none'))"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f'UPDATE "{AXIOM_SCHEMA}"."tasks" '
            "SET phase = 'waiting' WHERE phase = 'plan'"
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."tasks" '
            'DROP CONSTRAINT IF EXISTS "ck_tasks_phase"'
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."tasks" '
            "ADD CONSTRAINT \"ck_tasks_phase\" CHECK "
            "(phase IN ('bootstrap','reason','explore','waiting','none'))"
        )
    )
