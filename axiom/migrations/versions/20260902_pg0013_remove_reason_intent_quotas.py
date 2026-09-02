"""Remove obsolete Reason Intent volume quota columns (PostgreSQL branch)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260902_pg0013"
down_revision = "20260821_pg0012"
branch_labels = None
depends_on = None


_COLUMNS = ("target_open_intents", "max_new_intents")


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("reason_runs", schema=AXIOM_SCHEMA)
    }
    for name in _COLUMNS:
        if name in columns:
            op.drop_column("reason_runs", name, schema=AXIOM_SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("reason_runs", schema=AXIOM_SCHEMA)
    }
    for name in _COLUMNS:
        if name not in columns:
            op.add_column(
                "reason_runs",
                sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text("0")),
                schema=AXIOM_SCHEMA,
            )
