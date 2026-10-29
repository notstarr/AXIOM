"""Split report evidence into packet shots and business shots.

A reviewer's screenshots come in two kinds and they are not interchangeable.  A *packet*
shot shows the step's real request/response (the replay page, the proxy, the wire), and
it is what justifies believing the step at all.  A *business* shot shows the effect —
the admin table, the order page, the shell.  The gate in ``vulnerability_reviews`` asks
for one packet shot per reproduction step, so the two have to be distinguishable; a
single ``step_index`` column cannot express "this step has a picture, but not the one
that proves anything".

The default is ``business`` on purpose.  Rows written before this migration were
attached by hand with no such distinction, and calling them packet shots would let a
report satisfy the gate with pictures that never showed a packet.  They keep their step
placement; they simply do not count toward the gate.

``step_index`` is unchanged: 0 = an overview picture, >0 = the step it belongs to.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261029_pg0030"
down_revision = "20261027_pg0029"
branch_labels = None
depends_on = None


_TABLE = "report_evidence"
_KIND_DEFAULT = "business"
_KIND_CHECK = "report_evidence_kind"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=AXIOM_SCHEMA)


def _columns(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table, schema=AXIOM_SCHEMA)
    }


def _kind_check_names(table: str) -> list[str]:
    """Existing CHECK constraints that already constrain ``kind``.

    Matched by definition, not by name: the metadata naming convention renders
    ``report_evidence_kind`` as ``ck_report_evidence_report_evidence_kind``, and a
    database created straight from the models (as the migration tests do) already has
    that constraint before this revision runs.  Comparing names would then either miss
    it (and fail on a duplicate) or drop the wrong one on the way back down.
    """
    inspector = sa.inspect(op.get_bind())
    found: list[str] = []
    for item in inspector.get_check_constraints(table, schema=AXIOM_SCHEMA):
        name = item.get("name")
        clause = str(item.get("sqltext") or "").lower()
        if name and "kind" in clause and "packet" in clause:
            found.append(name)
    return found


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    if "kind" not in _columns(_TABLE):
        op.add_column(
            _TABLE,
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default=_KIND_DEFAULT,
            ),
            schema=AXIOM_SCHEMA,
        )
    if not _kind_check_names(_TABLE):
        op.create_check_constraint(
            _KIND_CHECK,
            _TABLE,
            "kind IN ('packet','business')",
            schema=AXIOM_SCHEMA,
        )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    for name in _kind_check_names(_TABLE):
        op.drop_constraint(name, _TABLE, schema=AXIOM_SCHEMA)
    if "kind" in _columns(_TABLE):
        op.drop_column(_TABLE, "kind", schema=AXIOM_SCHEMA)
