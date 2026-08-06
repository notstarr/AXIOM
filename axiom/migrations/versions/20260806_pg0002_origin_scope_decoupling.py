"""Decouple free-form Task Origin from network Scope and Intent targets.

Revision ID: 20260806_pg0002
Revises: 20260805_pg0001
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260806_pg0002"
down_revision = "20260805_pg0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "origin_host",
        schema=AXIOM_SCHEMA,
        existing_type=sa.String(),
        nullable=True,
    )
    # Disabled Scope entries never affected runtime access.  Canonicalize them
    # so the new list-driven UI cannot accidentally reactivate dormant lists.
    op.execute(
        sa.text(
            f"""
            UPDATE {AXIOM_SCHEMA}.tasks
               SET scope_json = '{{"enabled": false, "targets": [], "exclude": []}}'::jsonb
             WHERE NOT COALESCE((scope_json ->> 'enabled')::boolean, false)
                OR (
                    jsonb_array_length(COALESCE(scope_json -> 'targets', '[]'::jsonb)) = 0
                    AND jsonb_array_length(COALESCE(scope_json -> 'exclude', '[]'::jsonb)) = 0
                )
            """
        )
    )


def downgrade() -> None:
    # Old code requires a non-null network-shaped host.  A deterministic
    # sentinel keeps schema rollback possible for Tasks created with a
    # natural-language Origin; their original text remains in origin_input.
    op.execute(
        sa.text(
            f"""
            UPDATE {AXIOM_SCHEMA}.tasks
               SET origin_host = 'nonnetwork.invalid'
             WHERE origin_host IS NULL
            """
        )
    )
    op.alter_column(
        "tasks",
        "origin_host",
        schema=AXIOM_SCHEMA,
        existing_type=sa.String(),
        nullable=False,
    )
