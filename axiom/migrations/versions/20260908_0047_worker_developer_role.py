"""Add the probed developer-role capability to Workers."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260908_0047"
down_revision = "20260907_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("workers", schema="axiom"):
        return
    columns = {item["name"] for item in inspector.get_columns("workers", schema="axiom")}
    if "developer_role_supported" not in columns:
        op.add_column(
            "workers",
            sa.Column("developer_role_supported", sa.Boolean(), nullable=True),
            schema="axiom",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("workers", schema="axiom"):
        return
    columns = {item["name"] for item in inspector.get_columns("workers", schema="axiom")}
    if "developer_role_supported" in columns:
        op.drop_column("workers", "developer_role_supported", schema="axiom")
