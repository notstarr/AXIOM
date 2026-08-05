"""Add reversible task-level archiving without deleting execution history."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_0035"
down_revision = "20260804_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_index("ix_tasks_archived_status", ["archived", "status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_archived_status")
        batch.drop_column("archived")
