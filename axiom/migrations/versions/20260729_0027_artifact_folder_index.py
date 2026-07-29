"""Add the artifact folder navigation index."""

from __future__ import annotations

from alembic import op


revision = "20260729_0027"
down_revision = "20260729_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_artifacts_task_active_intent_created",
        "artifacts",
        ["task_id", "archived", "intent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifacts_task_active_intent_created",
        table_name="artifacts",
    )
