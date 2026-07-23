"""Create the relocatable Artifact metadata index."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0005"
down_revision = "20260723_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=True),
        sa.Column("artifact_type", sa.String(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_artifacts_task_id_tasks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "intent_id"],
            ["intents.task_id", "intents.id"],
            name="fk_artifacts_task_id_intent_id_intents",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", "id", name="pk_artifacts"),
        sa.UniqueConstraint("task_id", "storage_key", name="uq_artifacts_task_id_storage_key"),
    )


def downgrade() -> None:
    op.drop_table("artifacts")
