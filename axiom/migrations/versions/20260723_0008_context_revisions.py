"""Create progressive-disclosure Context revisions."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0008"
down_revision = "20260723_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_revisions",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=True),
        sa.Column("manifest_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_context_revisions_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "revision", name="pk_context_revisions"),
    )


def downgrade() -> None:
    op.drop_table("context_revisions")
