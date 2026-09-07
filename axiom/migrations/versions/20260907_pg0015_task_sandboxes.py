"""Add durable Task-scoped execution sandboxes to the PostgreSQL branch."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260907_pg0015"
down_revision = "20260902_pg0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("task_sandboxes", schema=AXIOM_SCHEMA):
        return
    op.create_table(
        "task_sandboxes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("sandbox_id", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False, server_default="local"),
        sa.Column("status", sa.String(), nullable=False, server_default="ready"),
        sa.Column("template_version", sa.String(), nullable=False, server_default="default"),
        sa.Column("resource_profile_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("network_policy_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("active_worker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminated_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id", name="pk_task_sandboxes"),
        sa.ForeignKeyConstraint(["task_id"], [f"{AXIOM_SCHEMA}.tasks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", name="uq_task_sandboxes_task_id"),
        sa.CheckConstraint("status IN ('creating','ready','degraded','terminating','terminated','failed')", name="ck_task_sandboxes_status"),
        schema=AXIOM_SCHEMA,
    )
    op.create_index("ix_task_sandboxes_status", "task_sandboxes", ["status"], schema=AXIOM_SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("task_sandboxes", schema=AXIOM_SCHEMA):
        return
    op.drop_index("ix_task_sandboxes_status", table_name="task_sandboxes", schema=AXIOM_SCHEMA)
    op.drop_table("task_sandboxes", schema=AXIOM_SCHEMA)
