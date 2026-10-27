"""Structured reproduction on findings/results, plus the late-evidence hook.

Two changes, both in service of "a report a human can walk again":

``reproduction_json`` on ``vulnerabilities`` and ``red_team_results``.  Explore used to
submit evidence *files* and a one-line curl command; the reviewer then reconstructed
the steps from prose, which is where fabricated or partial reproduction came from.  The
worker now submits the real packets at the moment it plays the exploit, so the record
is captured rather than remembered.

``report_evidence`` — explore no longer ships screenshots either.  A delivered report
still wants the one or two key pictures, but they are attached later, by a reviewer,
against a specific reproduction step.  The table covers both subject kinds (a
vulnerability and a red-team Result are the same shape here) so the renderer reads one
place.

Historical rows are intentionally NOT backfilled: ``'{}'`` means "no structured
reproduction", which the renderer treats as the legacy path.  Inventing steps for old
findings would reintroduce exactly the reconstruction this change removes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261027_pg0029"
down_revision = "20261026_pg0028"
branch_labels = None
depends_on = None


_REPRODUCTION_TABLES = ("vulnerabilities", "red_team_results")
_EVIDENCE_TABLE = "report_evidence"


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=AXIOM_SCHEMA)


def _columns(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table, schema=AXIOM_SCHEMA)
    }


def upgrade() -> None:
    for table in _REPRODUCTION_TABLES:
        if not _has_table(table):
            continue
        if "reproduction_json" not in _columns(table):
            op.add_column(
                table,
                sa.Column(
                    "reproduction_json",
                    sa.dialects.postgresql.JSONB(),
                    nullable=False,
                    server_default=sa.text("'{}'::jsonb"),
                ),
                schema=AXIOM_SCHEMA,
            )

    if _has_table(_EVIDENCE_TABLE):
        return
    op.create_table(
        _EVIDENCE_TABLE,
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "subject_type", "subject_id", "artifact_id"),
        sa.ForeignKeyConstraint(["task_id"], [f"{AXIOM_SCHEMA}.tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["task_id", "artifact_id"],
            [f"{AXIOM_SCHEMA}.artifacts.task_id", f"{AXIOM_SCHEMA}.artifacts.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "subject_type IN ('vulnerability','redteam_result')",
            name="report_evidence_subject_type",
        ),
        sa.CheckConstraint("step_index >= 0", name="report_evidence_step_index"),
        schema=AXIOM_SCHEMA,
    )
    op.create_index(
        "ix_report_evidence_subject",
        _EVIDENCE_TABLE,
        ["task_id", "subject_type", "subject_id"],
        schema=AXIOM_SCHEMA,
    )


def downgrade() -> None:
    if _has_table(_EVIDENCE_TABLE):
        bound = op.get_bind()
        indexes = {
            item["name"]
            for item in sa.inspect(bound).get_indexes(_EVIDENCE_TABLE, schema=AXIOM_SCHEMA)
        }
        if "ix_report_evidence_subject" in indexes:
            op.drop_index(
                "ix_report_evidence_subject",
                table_name=_EVIDENCE_TABLE,
                schema=AXIOM_SCHEMA,
            )
        op.drop_table(_EVIDENCE_TABLE, schema=AXIOM_SCHEMA)

    for table in _REPRODUCTION_TABLES:
        if not _has_table(table):
            continue
        if "reproduction_json" in _columns(table):
            op.drop_column(table, "reproduction_json", schema=AXIOM_SCHEMA)
