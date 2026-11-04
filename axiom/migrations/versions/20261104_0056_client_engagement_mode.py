"""Allow client engagement tasks on the generic/SQLite branch."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20261104_0056"
down_revision = "20261016_0055"
branch_labels = None
depends_on = None

_CLIENT_CHECK = "engagement_mode IN ('vulnerability','red_team','client')"
_LEGACY_CHECK = "engagement_mode IN ('vulnerability','red_team')"
_CONSTRAINT = "ck_tasks_engagement_mode"


def _engagement_check_names(bind) -> list[str]:
    names: list[str] = []
    for constraint in sa.inspect(bind).get_check_constraints("tasks"):
        name = constraint.get("name")
        clause = str(constraint.get("sqltext") or "").lower()
        if name and "engagement_mode" in clause:
            names.append(name)
    return names


def _replace_engagement_check(expression: str) -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tasks"):
        return
    with op.batch_alter_table("tasks", recreate="always") as batch:
        for name in _engagement_check_names(bind):
            batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(op.f(_CONSTRAINT), expression)


def _reject_client_rows_for_downgrade(bind) -> None:
    count = int(
        bind.execute(
            sa.text("SELECT count(*) FROM tasks WHERE engagement_mode = 'client'")
        ).scalar_one()
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade client engagement mode while client tasks exist"
        )


def upgrade() -> None:
    _replace_engagement_check(_CLIENT_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("tasks"):
        return
    _reject_client_rows_for_downgrade(bind)
    _replace_engagement_check(_LEGACY_CHECK)
