"""Persist Goal assessments as the only Task completion authority."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0015"
down_revision = "20260723_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goal_evaluations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=True),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("achieved", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("evaluator", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_goal_evaluations_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_goal_evaluations"),
    )
    op.create_index(
        "ix_goal_evaluations_task_created",
        "goal_evaluations",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_goal_evaluations_task_created", table_name="goal_evaluations")
    op.drop_table("goal_evaluations")
