"""Allow client engagement tasks on the PostgreSQL branch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261104_pg0032"
down_revision = "20261103_pg0031"
branch_labels = None
depends_on = None

_CLIENT_CHECK = "engagement_mode IN ('vulnerability','red_team','client')"
_LEGACY_CHECK = "engagement_mode IN ('vulnerability','red_team')"
_CONSTRAINT = "ck_tasks_engagement_mode"


def _engagement_check_names() -> list[str]:
    names: list[str] = []
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_check_constraints("tasks", schema=AXIOM_SCHEMA):
        name = constraint.get("name")
        clause = str(constraint.get("sqltext") or "").lower()
        if name and "engagement_mode" in clause:
            names.append(name)
    return names


def _replace_engagement_check(expression: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tasks", schema=AXIOM_SCHEMA):
        return
    for name in _engagement_check_names():
        op.drop_constraint(
            op.f(name),
            "tasks",
            type_="check",
            schema=AXIOM_SCHEMA,
        )
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        "tasks",
        expression,
        schema=AXIOM_SCHEMA,
    )


def upgrade() -> None:
    _replace_engagement_check(_CLIENT_CHECK)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tasks", schema=AXIOM_SCHEMA):
        return
    count = int(
        op.get_bind()
        .execute(
            sa.text(
                f'SELECT count(*) FROM "{AXIOM_SCHEMA}".tasks '
                "WHERE engagement_mode = 'client'"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade client engagement mode while client tasks exist"
        )
    _replace_engagement_check(_LEGACY_CHECK)
