"""Add Context revision storage lifecycle metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261012_0048"
down_revision = "20260908_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("context_revisions", schema="axiom"):
        return
    columns = {item["name"] for item in inspector.get_columns("context_revisions", schema="axiom")}
    if "storage_state" not in columns:
        op.add_column(
            "context_revisions",
            sa.Column("storage_state", sa.String(), nullable=False, server_default="present"),
            schema="axiom",
        )
    if "deleted_at" not in columns:
        op.add_column(
            "context_revisions",
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            schema="axiom",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("context_revisions", schema="axiom"):
        return
    columns = {item["name"] for item in inspector.get_columns("context_revisions", schema="axiom")}
    if "deleted_at" in columns:
        op.drop_column("context_revisions", "deleted_at", schema="axiom")
    if "storage_state" in columns:
        op.drop_column("context_revisions", "storage_state", schema="axiom")
