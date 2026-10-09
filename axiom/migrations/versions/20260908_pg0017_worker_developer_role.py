"""Add the probed developer-role capability to PostgreSQL Workers."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260908_pg0017"
down_revision = "20260907_pg0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("workers", schema=AXIOM_SCHEMA):
        return
    columns = {item["name"] for item in inspector.get_columns("workers", schema=AXIOM_SCHEMA)}
    if "developer_role_supported" not in columns:
        op.add_column(
            "workers",
            sa.Column("developer_role_supported", sa.Boolean(), nullable=True),
            schema=AXIOM_SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("workers", schema=AXIOM_SCHEMA):
        return
    columns = {item["name"] for item in inspector.get_columns("workers", schema=AXIOM_SCHEMA)}
    if "developer_role_supported" in columns:
        op.drop_column("workers", "developer_role_supported", schema=AXIOM_SCHEMA)
