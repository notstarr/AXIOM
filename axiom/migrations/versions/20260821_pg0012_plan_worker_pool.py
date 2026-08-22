"""Add an independent Plan Worker pool to Tasks."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260821_pg0012"
down_revision = "20260821_pg0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("tasks", schema=AXIOM_SCHEMA)
    }
    if "plan_worker_pool_json" not in columns:
        op.add_column(
            "tasks",
            sa.Column(
                "plan_worker_pool_json",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            schema=AXIOM_SCHEMA,
        )
    else:
        # A development build briefly shipped this revision with Text.  Make
        # an already-created column converge to the same JSONB contract as
        # the other Task JSON fields without losing existing pool values.
        column = next(
            item
            for item in sa.inspect(bind).get_columns(
                "tasks", schema=AXIOM_SCHEMA
            )
            if item["name"] == "plan_worker_pool_json"
        )
        if str(column.get("type", "")).lower() in {"text", "varchar"}:
            op.execute(
                sa.text(
                    f'ALTER TABLE "{AXIOM_SCHEMA}"."tasks" '
                    'ALTER COLUMN "plan_worker_pool_json" TYPE JSONB '
                    'USING CASE '
                    'WHEN trim(COALESCE("plan_worker_pool_json", \'\')) = \'\' '
                    "THEN '[]'::jsonb "
                    'ELSE "plan_worker_pool_json"::jsonb END'
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("tasks", schema=AXIOM_SCHEMA)
    }
    if "plan_worker_pool_json" in columns:
        op.drop_column("tasks", "plan_worker_pool_json", schema=AXIOM_SCHEMA)
