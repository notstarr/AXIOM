"""Add editable Project Knowledge records."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0014"
down_revision = "20260723_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_knowledge",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_knowledge_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "id", name="pk_project_knowledge"),
    )
    op.create_index(
        "ix_project_knowledge_project_updated",
        "project_knowledge",
        ["project_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_project_knowledge_project_updated", table_name="project_knowledge")
    op.drop_table("project_knowledge")
