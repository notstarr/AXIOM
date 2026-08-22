"""Add an independent Plan Worker pool to Tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260821_0043"
down_revision = "20260821_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("tasks")}
    if "plan_worker_pool_json" not in columns:
        op.add_column(
            "tasks",
            sa.Column(
                "plan_worker_pool_json",
                sa.Text(),
                nullable=False,
                server_default="[]",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("tasks")}
    if "plan_worker_pool_json" in columns:
        op.drop_column("tasks", "plan_worker_pool_json")
