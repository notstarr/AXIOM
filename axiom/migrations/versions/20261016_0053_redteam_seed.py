"""Add red-team exploitable marker and task seed link (SQLite branch).

SQLite cannot attach a foreign key to an existing table without a table
rebuild, so this branch only adds the columns; the self-referential
ON DELETE SET NULL constraint exists in the PostgreSQL branch and in the
ORM metadata for freshly created databases.  Dangling seed ids on legacy
SQLite installs are handled defensively by the context builder.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261016_0053"
down_revision = "20261015_0052"
branch_labels = None
depends_on = None


_ADD = (
    ("vulnerabilities", "red_team_exploitable", sa.Column(
        "red_team_exploitable",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )),
    ("vulnerabilities", "red_team_exploitable_at", sa.Column(
        "red_team_exploitable_at",
        sa.DateTime(timezone=True),
        nullable=True,
    )),
    ("vulnerabilities", "red_team_exploitable_by", sa.Column(
        "red_team_exploitable_by",
        sa.String(),
        nullable=True,
    )),
    ("tasks", "seed_vulnerability_task_id", sa.Column(
        "seed_vulnerability_task_id",
        sa.String(),
        nullable=True,
    )),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, name, column in _ADD:
        if not inspector.has_table(table, schema=AXIOM_SCHEMA):
            continue
        existing = {item["name"] for item in inspector.get_columns(table, schema=AXIOM_SCHEMA)}
        if name not in existing:
            op.add_column(table, column, schema=AXIOM_SCHEMA)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, name, _column in reversed(_ADD):
        if not inspector.has_table(table, schema=AXIOM_SCHEMA):
            continue
        existing = {item["name"] for item in inspector.get_columns(table, schema=AXIOM_SCHEMA)}
        if name in existing:
            op.drop_column(table, name, schema=AXIOM_SCHEMA)
