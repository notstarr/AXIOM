"""Add retest verdict columns and allow the ``retest`` trigger (PostgreSQL branch).

与 generic 分支同构，只是走原生 ALTER。列可空、无 server_default，和 ``tables.py``
保持一致——``migrations/env.py`` 开了 ``compare_server_default``，任何默认值差异都会
被 drift 测试抓到。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261106_pg0033"
down_revision = "20261104_pg0032"
branch_labels = None
depends_on = None

_TABLE = "vulnerability_review_runs"
_TRIGGER_CHECK = "ck_vulnerability_review_runs_trigger"
_RETEST_CHECK = "ck_vulnerability_review_runs_retest_verdict"
_NEW_TRIGGER = "trigger IN ('reported','updated','manual','restored','migration','retest')"
_OLD_TRIGGER = "trigger IN ('reported','updated','manual','restored','migration')"
_RETEST_EXPRESSION = (
    "retest_verdict IS NULL OR retest_verdict IN ('exists','fixed','inconclusive')"
)
_NEW_COLUMNS = (
    ("retest_verdict", sa.String()),
    ("retest_reason", sa.Text()),
)


def _has_table() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE, schema=AXIOM_SCHEMA)


def _columns() -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=AXIOM_SCHEMA)
    }


def _check_names(*needles: str) -> list[str]:
    names: list[str] = []
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_check_constraints(_TABLE, schema=AXIOM_SCHEMA):
        name = constraint.get("name")
        clause = str(constraint.get("sqltext") or "").lower()
        if name and all(needle in clause for needle in needles):
            names.append(name)
    return names


def upgrade() -> None:
    if not _has_table():
        return
    existing = _columns()
    for name, column_type in _NEW_COLUMNS:
        if name not in existing:
            op.add_column(
                _TABLE,
                sa.Column(name, column_type, nullable=True),
                schema=AXIOM_SCHEMA,
            )
    for name in _check_names("trigger", "migration"):
        op.drop_constraint(op.f(name), _TABLE, type_="check", schema=AXIOM_SCHEMA)
    op.create_check_constraint(
        op.f(_TRIGGER_CHECK), _TABLE, _NEW_TRIGGER, schema=AXIOM_SCHEMA
    )
    if not _check_names("retest_verdict"):
        op.create_check_constraint(
            op.f(_RETEST_CHECK), _TABLE, _RETEST_EXPRESSION, schema=AXIOM_SCHEMA
        )


def downgrade() -> None:
    if not _has_table():
        return
    count = int(
        op.get_bind()
        .execute(
            sa.text(
                f'SELECT count(*) FROM "{AXIOM_SCHEMA}".{_TABLE} '
                "WHERE trigger = 'retest' OR retest_verdict IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade retest support while retest runs exist"
        )
    existing = _columns()
    for name in _check_names("retest_verdict"):
        op.drop_constraint(op.f(name), _TABLE, type_="check", schema=AXIOM_SCHEMA)
    for name in _check_names("trigger", "migration"):
        op.drop_constraint(op.f(name), _TABLE, type_="check", schema=AXIOM_SCHEMA)
    op.create_check_constraint(
        op.f(_TRIGGER_CHECK), _TABLE, _OLD_TRIGGER, schema=AXIOM_SCHEMA
    )
    for name, _ in _NEW_COLUMNS:
        if name in existing:
            op.drop_column(_TABLE, name, schema=AXIOM_SCHEMA)
