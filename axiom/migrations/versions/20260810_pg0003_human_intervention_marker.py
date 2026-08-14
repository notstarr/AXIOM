"""Add the durable Explore human-intervention marker to Intent.

Revision ID: 20260810_pg0003
Revises: 20260806_pg0002
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260810_pg0003"
down_revision = "20260806_pg0002"
branch_labels = None
depends_on = None


def _intent_columns() -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(
            "intents",
            schema=AXIOM_SCHEMA,
        )
    }


def upgrade() -> None:
    existing = _intent_columns()
    if "human_intervention_required" not in existing:
        op.add_column(
            "intents",
            sa.Column(
                "human_intervention_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            schema=AXIOM_SCHEMA,
        )
    if "human_intervention_reason" not in existing:
        op.add_column(
            "intents",
            sa.Column(
                "human_intervention_reason",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
            schema=AXIOM_SCHEMA,
        )
    if "human_intervention_marked_at" not in existing:
        op.add_column(
            "intents",
            sa.Column("human_intervention_marked_at", sa.DateTime(timezone=True)),
            schema=AXIOM_SCHEMA,
        )


def downgrade() -> None:
    existing = _intent_columns()
    if "human_intervention_marked_at" in existing:
        op.drop_column(
            "intents",
            "human_intervention_marked_at",
            schema=AXIOM_SCHEMA,
        )
    if "human_intervention_reason" in existing:
        op.drop_column(
            "intents",
            "human_intervention_reason",
            schema=AXIOM_SCHEMA,
        )
    if "human_intervention_required" in existing:
        op.drop_column(
            "intents",
            "human_intervention_required",
            schema=AXIOM_SCHEMA,
        )
