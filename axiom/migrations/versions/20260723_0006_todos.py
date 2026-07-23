"""Create the shared single-level Todo list."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0006"
down_revision = "20260723_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "todos",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("theme", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "category IN ('recon','authentication','authorization','injection',"
            "'business_logic','file','network','configuration','client_side','other')",
            name="ck_todos_category",
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name="ck_todos_priority"),
        sa.CheckConstraint(
            "status IN ('pending','active','completed','rejected','obsolete')",
            name="ck_todos_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_todos_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id", "id", name="pk_todos"),
    )
    op.create_index(
        "ix_todos_task_status_priority",
        "todos",
        ["task_id", "status", "priority"],
        unique=False,
    )
    op.create_table(
        "todo_sources",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("todo_id", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('fact','hint','artifact','vulnerability','knowledge','origin')",
            name="ck_todo_sources_source_type",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "todo_id"],
            ["todos.task_id", "todos.id"],
            name="fk_todo_sources_task_id_todo_id_todos",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_id", "todo_id", "source_type", "source_id", name="pk_todo_sources"
        ),
    )


def downgrade() -> None:
    op.drop_table("todo_sources")
    op.drop_index("ix_todos_task_status_priority", table_name="todos")
    op.drop_table("todos")
