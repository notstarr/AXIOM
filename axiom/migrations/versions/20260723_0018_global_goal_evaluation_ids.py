"""Seed the global Goal Evaluation identifier sequence."""

import re

from alembic import op
import sqlalchemy as sa


revision = "20260723_0018"
down_revision = "20260723_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    maximum = 0
    for value in connection.execute(sa.text("SELECT id FROM goal_evaluations")).scalars():
        match = re.fullmatch(r"ge(\d+)", str(value or ""))
        if match:
            maximum = max(maximum, int(match.group(1)))
    next_value = maximum + 1
    connection.execute(
        sa.text(
            """
            INSERT INTO id_counters(scope, kind, next_value)
            VALUES ('global', 'goal_evaluation', :next_value)
            ON CONFLICT(scope, kind) DO UPDATE SET
                next_value = MAX(id_counters.next_value, excluded.next_value)
            """
        ),
        {"next_value": next_value},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "DELETE FROM id_counters "
            "WHERE scope = 'global' AND kind = 'goal_evaluation'"
        )
    )
