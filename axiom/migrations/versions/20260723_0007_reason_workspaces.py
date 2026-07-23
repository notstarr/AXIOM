"""Create Reason Workspace persistence."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0007"
down_revision = "20260723_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reason_workspaces",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("context_revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False),
        sa.Column("worker", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("committed_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open','committed','abandoned')",
            name="ck_reason_workspaces_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_reason_workspaces_task_id_tasks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "intent_id"],
            ["intents.task_id", "intents.id"],
            name="fk_reason_workspaces_task_id_intent_id_intents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reason_workspaces"),
        sa.UniqueConstraint(
            "task_id", "intent_id", name="uq_reason_workspaces_task_id_intent_id"
        ),
    )
    op.create_index(
        "ix_reason_workspaces_task_status",
        "reason_workspaces",
        ["task_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reason_workspaces_task_status", table_name="reason_workspaces")
    op.drop_table("reason_workspaces")
