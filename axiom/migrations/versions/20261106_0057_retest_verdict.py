"""Add retest verdict columns and allow the ``retest`` trigger (generic/SQLite branch).

复测复用同一张 ``vulnerability_review_runs``，但结论与审核裁决分列存放：``decision``
继续只承载 verified/duplicate/ignored/needs-human，复测写 ``retest_verdict``。
两列可空、无 server_default——``metadata`` 里的列也是这样定义的，迁移必须与之一致，
否则 ``compare_metadata`` 会把新列报成 drift。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20261106_0057"
down_revision = "20261104_0056"
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


def _has_table(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def _columns(bind) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_columns(_TABLE)}


def _check_names(bind, *needles: str) -> list[str]:
    """Match constraints by their SQL text, not by name.

    The naming convention renders ``trigger`` as ``ck_vulnerability_review_runs_trigger``,
    but a database created straight from ``metadata`` and one created by an older
    migration can disagree on the name.  The clause is what actually has to change.
    """
    names: list[str] = []
    for constraint in sa.inspect(bind).get_check_constraints(_TABLE):
        name = constraint.get("name")
        clause = str(constraint.get("sqltext") or "").lower()
        if name and all(needle in clause for needle in needles):
            names.append(name)
    return names


def _reject_retest_rows(bind) -> None:
    count = int(
        bind.execute(
            sa.text(
                f"SELECT count(*) FROM {_TABLE} "
                "WHERE trigger = 'retest' OR retest_verdict IS NOT NULL"
            )
        ).scalar_one()
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade retest support while retest runs exist"
        )


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind):
        return
    existing = _columns(bind)
    trigger_checks = _check_names(bind, "trigger", "migration")
    has_retest_check = bool(_check_names(bind, "retest_verdict"))

    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        for name, column_type in _NEW_COLUMNS:
            if name not in existing:
                batch.add_column(sa.Column(name, column_type, nullable=True))
        for name in trigger_checks:
            batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(op.f(_TRIGGER_CHECK), _NEW_TRIGGER)
        if not has_retest_check:
            batch.create_check_constraint(op.f(_RETEST_CHECK), _RETEST_EXPRESSION)


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind):
        return
    _reject_retest_rows(bind)
    existing = _columns(bind)
    trigger_checks = _check_names(bind, "trigger", "migration")
    retest_checks = _check_names(bind, "retest_verdict")

    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        for name in retest_checks:
            batch.drop_constraint(op.f(name), type_="check")
        for name in trigger_checks:
            batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(op.f(_TRIGGER_CHECK), _OLD_TRIGGER)
        for name, _ in _NEW_COLUMNS:
            if name in existing:
                batch.drop_column(name)
