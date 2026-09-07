"""Add runtime metadata and healthcheck fields to Task sandboxes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260907_0046"
down_revision = "20260907_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("task_sandboxes", schema="axiom"):
        return
    columns = {item["name"] for item in inspector.get_columns("task_sandboxes", schema="axiom")}
    if "runtime_revision" not in columns:
        op.add_column(
            "task_sandboxes",
            sa.Column("runtime_revision", sa.String(), nullable=False, server_default="legacy-local"),
            schema="axiom",
        )
    if "remote_root" not in columns:
        op.add_column(
            "task_sandboxes",
            sa.Column("remote_root", sa.String(), nullable=False, server_default="/workspace/axiom"),
            schema="axiom",
        )
    if "last_healthcheck_at" not in columns:
        op.add_column(
            "task_sandboxes",
            sa.Column("last_healthcheck_at", sa.DateTime(timezone=True), nullable=True),
            schema="axiom",
        )
    if "last_healthcheck_error" not in columns:
        op.add_column(
            "task_sandboxes",
            sa.Column("last_healthcheck_error", sa.Text(), nullable=False, server_default=""),
            schema="axiom",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("task_sandboxes", schema="axiom"):
        return
    columns = {item["name"] for item in inspector.get_columns("task_sandboxes", schema="axiom")}
    for name in ("last_healthcheck_error", "last_healthcheck_at", "remote_root", "runtime_revision"):
        if name in columns:
            op.drop_column("task_sandboxes", name, schema="axiom")
