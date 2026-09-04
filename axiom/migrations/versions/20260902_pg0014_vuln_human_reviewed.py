"""Add human-review toggle columns to vulnerabilities (PostgreSQL branch)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260902_pg0014"
down_revision = "20260902_pg0013"
branch_labels = None
depends_on = None


_ADD = (
    ("human_reviewed", sa.Column(
        "human_reviewed",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )),
    ("human_reviewed_at", sa.Column(
        "human_reviewed_at",
        sa.DateTime(timezone=True),
        nullable=True,
    )),
    ("human_reviewed_by", sa.Column(
        "human_reviewed_by",
        sa.String(),
        nullable=True,
    )),
)


def _columns(bind) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(bind).get_columns("vulnerabilities", schema=AXIOM_SCHEMA)
    }


def upgrade() -> None:
    existing = _columns(op.get_bind())
    for name, column in _ADD:
        if name not in existing:
            op.add_column("vulnerabilities", column, schema=AXIOM_SCHEMA)


def downgrade() -> None:
    existing = _columns(op.get_bind())
    for name, _column in reversed(_ADD):
        if name in existing:
            op.drop_column("vulnerabilities", name, schema=AXIOM_SCHEMA)
