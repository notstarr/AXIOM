"""Add the cross-process human-to-Explore intervention queue."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260804_0033"
down_revision = "20260804_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intent_interventions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False, server_default="human.frontend"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("client_message_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("sent_at", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id", "intent_id"], ["intents.task_id", "intents.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('pending','sent','accepted','failed','expired')", name="status"),
    )
    op.create_index(
        "uq_intent_interventions_client",
        "intent_interventions",
        ["task_id", "intent_id", "client_message_id"],
        unique=True,
    )
    op.create_index(
        "ix_intent_interventions_queue",
        "intent_interventions",
        ["task_id", "intent_id", "lease_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_intent_interventions_queue", table_name="intent_interventions")
    op.drop_index("uq_intent_interventions_client", table_name="intent_interventions")
    op.drop_table("intent_interventions")
