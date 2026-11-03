"""Give RedTeam Results a duplicate target of their own.

A RedTeam Result can be ``duplicate`` — the finding holds, but it lands on an asset
that has already been scored, so it counts once.  That status has been in the
``red_team_results`` CHECK constraint since the table was created, and the console
renders it (``♻️ 重复``).  It was unreachable in practice: the review command
required ``--duplicate-of`` pointing at a *Vulnerability* (a ``vXXX``), and the
service refused a RedTeam Result carrying a Vulnerability target at all.  A
Reviewer who spotted the duplicate had no way to say so, so they filed
``ignored`` (which lands as ``rejected``) and spelled out "this is a duplicate"
in the free-text reason.  Reports then could not tell "not established" apart
from "established, already counted", and a few rows were simply scored twice.

The target of a duplicate is a *Result* here, not a Vulnerability, and a Result
is not a Vulnerability entity, so the existing ``duplicate_of_vulnerability_id``
foreign key cannot carry it.  This revision adds ``duplicate_of_result_id`` to
``vulnerability_review_runs``: a composite foreign key onto
``red_team_results(task_id, id)``, mirroring the Vulnerability column's shape.

The column stays nullable on purpose.  "One asset counts once" is a coarse
rule, and a Reviewer who cannot name the twin should still be able to file
``duplicate`` and explain it in the reason instead of inventing a target.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261103_pg0031"
down_revision = "20261029_pg0030"
branch_labels = None
depends_on = None


_TABLE = "vulnerability_review_runs"
_COLUMN = "duplicate_of_result_id"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=AXIOM_SCHEMA)


def _columns(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table, schema=AXIOM_SCHEMA)
    }


def _foreign_keys(table: str) -> list[dict]:
    return list(
        sa.inspect(op.get_bind()).get_foreign_keys(table, schema=AXIOM_SCHEMA)
    )


def _has_result_fk(table: str) -> bool:
    """Whether the composite Result foreign key is already present.

    Matched by column, not by name: a database created straight from the models
    (as the migration tests do) already carries the constraint before this
    revision runs, and comparing names would then either miss it or drop the
    wrong one on the way back down.
    """
    for item in _foreign_keys(table):
        constrained = set(item.get("constrained_columns") or [])
        referred = set(item.get("referred_columns") or [])
        if _COLUMN in constrained and "id" in referred:
            return True
    return False


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    if _COLUMN not in _columns(_TABLE):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(), nullable=True),
            schema=AXIOM_SCHEMA,
        )
    if not _has_result_fk(_TABLE):
        op.create_foreign_key(
            "fk_vulnerability_review_runs_task_id_duplicate_of_result",
            _TABLE,
            "red_team_results",
            ["task_id", _COLUMN],
            ["task_id", "id"],
            source_schema=AXIOM_SCHEMA,
            referent_schema=AXIOM_SCHEMA,
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    if _has_result_fk(_TABLE):
        op.drop_constraint(
            "fk_vulnerability_review_runs_task_id_duplicate_of_result",
            _TABLE,
            schema=AXIOM_SCHEMA,
            type_="foreignkey",
        )
    if _COLUMN in _columns(_TABLE):
        op.drop_column(_TABLE, _COLUMN, schema=AXIOM_SCHEMA)
