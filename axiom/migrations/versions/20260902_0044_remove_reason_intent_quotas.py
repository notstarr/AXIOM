"""Remove obsolete Reason Intent volume quota columns (generic branch)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260902_0044"
down_revision = "20260821_0043"
branch_labels = None
depends_on = None


_COLUMNS = ("target_open_intents", "max_new_intents")


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("reason_runs")}
    if bind.dialect.name == "sqlite":
        existing = [name for name in _COLUMNS if name in columns]
        if not existing:
            return
        with op.batch_alter_table("reason_runs", recreate="always") as batch:
            for name in existing:
                batch.drop_column(name)
        return
    for name in _COLUMNS:
        if name in columns:
            op.drop_column("reason_runs", name)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("reason_runs")}
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("reason_runs") as batch:
            for name in _COLUMNS:
                if name not in columns:
                    batch.add_column(sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
        return
    for name in _COLUMNS:
        if name not in columns:
            op.add_column(
                "reason_runs",
                sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
            )
