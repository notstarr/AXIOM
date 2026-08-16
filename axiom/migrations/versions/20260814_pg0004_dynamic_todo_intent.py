"""Add canonical Todo lifecycle and durable Intent assessment fields."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260814_pg0004"
down_revision = "20260810_pg0003"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table, schema=AXIOM_SCHEMA)
    }


def _drop_constraint(table: str, name: str) -> None:
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."{table}" '
            f'DROP CONSTRAINT IF EXISTS "{name}"'
        )
    )


def upgrade() -> None:
    existing = _columns("intents")
    additions = (
        ("priority", sa.Column("priority", sa.Integer(), nullable=False, server_default="50")),
        ("assessment_status", sa.Column("assessment_status", sa.String(), nullable=False, server_default="open")),
        ("assessment_reason", sa.Column("assessment_reason", sa.Text(), nullable=False, server_default="")),
        ("assessed_at", sa.Column("assessed_at", sa.DateTime(timezone=True))),
    )
    for name, column in additions:
        if name not in existing:
            op.add_column("intents", column, schema=AXIOM_SCHEMA)

    # Existing installations may have either the named Alembic constraint or
    # the metadata-generated name.  Drop both before installing the canonical
    # check so upgrades remain idempotent across those installations.
    for name in ("ck_todos_status", "status"):
        _drop_constraint("todos", name)
    for name in ("ck_intents_priority", "priority"):
        _drop_constraint("intents", name)
    for name in ("ck_intents_assessment_status", "assessment_status"):
        _drop_constraint("intents", name)

    op.alter_column(
        "todos",
        "status",
        schema=AXIOM_SCHEMA,
        existing_type=sa.String(),
        server_default="open",
    )

    # Drop the legacy Todo check before changing values.  This ordering is
    # required for installations that still contain pending/active rows.
    op.execute(
        sa.text(
            f"UPDATE \"{AXIOM_SCHEMA}\".todos SET status = 'open' "
            "WHERE status = 'pending'"
        )
    )
    # ``active`` was the legacy in-progress Todo state.  The canonical
    # lifecycle calls that state ``partial`` regardless of whether its first
    # Explore Intent has already reached a terminal execution state; the
    # latter is an execution detail, not a Todo lifecycle transition.
    op.execute(
        sa.text(
            f"UPDATE \"{AXIOM_SCHEMA}\".todos SET status = 'partial' "
            "WHERE status = 'active'"
        )
    )
    op.execute(
        sa.text(
            f"UPDATE \"{AXIOM_SCHEMA}\".todos SET status_reason = '从旧 Todo 状态迁移' "
            "WHERE status IN ('partial','blocked','completed') "
            "AND trim(COALESCE(status_reason, '')) = ''"
        )
    )
    # A Todo's completion timestamp is meaningful only for the completed
    # lifecycle state. Clean up stale legacy timestamps while normalizing
    # pending/active rows (and any other non-completed historical values).
    op.execute(
        sa.text(
            f"UPDATE \"{AXIOM_SCHEMA}\".todos SET completed_at = NULL "
            "WHERE status <> 'completed'"
        )
    )

    op.create_check_constraint(
        "status",
        "todos",
        "status IN ('open','partial','blocked','completed','rejected','obsolete')",
        schema=AXIOM_SCHEMA,
    )
    op.create_check_constraint(
        "priority",
        "intents",
        "priority BETWEEN 0 AND 100",
        schema=AXIOM_SCHEMA,
    )
    op.create_check_constraint(
        "assessment_status",
        "intents",
        "assessment_status IN ('open','partial','blocked','completed')",
        schema=AXIOM_SCHEMA,
    )


def downgrade() -> None:
    for name in ("ck_todos_status", "status"):
        _drop_constraint("todos", name)
    for name in ("ck_intents_priority", "priority"):
        _drop_constraint("intents", name)
    for name in ("ck_intents_assessment_status", "assessment_status"):
        _drop_constraint("intents", name)
    op.execute(
        sa.text(
            f"UPDATE \"{AXIOM_SCHEMA}\".todos SET status = 'pending' "
            "WHERE status = 'open'"
        )
    )
    op.execute(
        sa.text(
            f"UPDATE \"{AXIOM_SCHEMA}\".todos SET status = 'active' "
            "WHERE status IN ('partial','blocked')"
        )
    )
    # Add the legacy constraint only after every canonical-only value has been
    # mapped.  PostgreSQL validates existing rows when a CHECK is created, so
    # creating it while any ``partial``/``blocked`` rows remain would make a
    # downgrade fail on real historical data.
    op.create_check_constraint(
        "status",
        "todos",
        "status IN ('pending','active','completed','rejected','obsolete')",
        schema=AXIOM_SCHEMA,
    )
    op.alter_column(
        "todos",
        "status",
        schema=AXIOM_SCHEMA,
        existing_type=sa.String(),
        server_default=None,
    )
    existing = _columns("intents")
    for name in ("assessed_at", "assessment_reason", "assessment_status", "priority"):
        if name in existing:
            op.drop_column("intents", name, schema=AXIOM_SCHEMA)
