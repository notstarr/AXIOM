"""Add runtime metadata and healthcheck fields to PostgreSQL Task sandboxes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260907_pg0016"
down_revision = "20260907_pg0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("task_sandboxes", schema=AXIOM_SCHEMA):
        return
    columns = {item["name"] for item in inspector.get_columns("task_sandboxes", schema=AXIOM_SCHEMA)}
    if "runtime_revision" not in columns:
        op.add_column("task_sandboxes", sa.Column("runtime_revision", sa.String(), nullable=False, server_default="legacy-local"), schema=AXIOM_SCHEMA)
    if "remote_root" not in columns:
        op.add_column("task_sandboxes", sa.Column("remote_root", sa.String(), nullable=False, server_default="/workspace/axiom"), schema=AXIOM_SCHEMA)
    if "last_healthcheck_at" not in columns:
        op.add_column("task_sandboxes", sa.Column("last_healthcheck_at", sa.DateTime(timezone=True)), schema=AXIOM_SCHEMA)
    if "last_healthcheck_error" not in columns:
        op.add_column("task_sandboxes", sa.Column("last_healthcheck_error", sa.Text(), nullable=False, server_default=""), schema=AXIOM_SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("task_sandboxes", schema=AXIOM_SCHEMA):
        return
    columns = {item["name"] for item in inspector.get_columns("task_sandboxes", schema=AXIOM_SCHEMA)}
    for name in ("last_healthcheck_error", "last_healthcheck_at", "remote_root", "runtime_revision"):
        if name in columns:
            op.drop_column("task_sandboxes", name, schema=AXIOM_SCHEMA)
