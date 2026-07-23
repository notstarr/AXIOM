"""add Reason live activity fields

Revision ID: f62b8d1e4a90
Revises: d34f91a2bc08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f62b8d1e4a90"
down_revision: Union[str, Sequence[str], None] = "d34f91a2bc08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("reason_activity_kind", sa.String(), server_default="", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("reason_activity_summary", sa.Text(), server_default="", nullable=False),
    )
    op.add_column("tasks", sa.Column("reason_activity_started_at", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("reason_activity_updated_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "reason_activity_updated_at")
    op.drop_column("tasks", "reason_activity_started_at")
    op.drop_column("tasks", "reason_activity_summary")
    op.drop_column("tasks", "reason_activity_kind")
