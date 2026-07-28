"""Add per-run Task deadlines and automatic pause metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260728_0024"
down_revision = "20260727_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("run_duration_seconds", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("run_started_at", sa.String(), nullable=True))
        batch.add_column(sa.Column("run_deadline_at", sa.String(), nullable=True))
        batch.add_column(sa.Column("last_pause_reason", sa.String(), nullable=True))
        batch.create_check_constraint(
            "ck_tasks_run_duration_seconds",
            "run_duration_seconds IS NULL OR "
            "(run_duration_seconds BETWEEN 60 AND 31536000 "
            "AND run_duration_seconds % 60 = 0)",
        )
        batch.create_check_constraint(
            "ck_tasks_last_pause_reason",
            "last_pause_reason IS NULL OR "
            "last_pause_reason IN ('manual','run_deadline_reached')",
        )
    op.create_index(
        "ix_tasks_status_run_deadline",
        "tasks",
        ["status", "run_deadline_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_status_run_deadline", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("ck_tasks_last_pause_reason", type_="check")
        batch.drop_constraint("ck_tasks_run_duration_seconds", type_="check")
        batch.drop_column("last_pause_reason")
        batch.drop_column("run_deadline_at")
        batch.drop_column("run_started_at")
        batch.drop_column("run_duration_seconds")
