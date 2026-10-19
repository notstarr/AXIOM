"""Add red-team exploitable marker and task seed link (PostgreSQL branch)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261016_pg0023"
down_revision = "20261015_pg0022"
branch_labels = None
depends_on = None


_VULN_ADD = (
    ("red_team_exploitable", sa.Column(
        "red_team_exploitable",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )),
    ("red_team_exploitable_at", sa.Column(
        "red_team_exploitable_at",
        sa.DateTime(timezone=True),
        nullable=True,
    )),
    ("red_team_exploitable_by", sa.Column(
        "red_team_exploitable_by",
        sa.String(),
        nullable=True,
    )),
)

_FK_NAME = "fk_tasks_seed_vulnerability_task_id_tasks"


def _columns(table: str) -> set[str]:
    return {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns(table, schema=AXIOM_SCHEMA)
    }


def upgrade() -> None:
    vuln_cols = _columns("vulnerabilities")
    for name, column in _VULN_ADD:
        if name not in vuln_cols:
            op.add_column("vulnerabilities", column, schema=AXIOM_SCHEMA)

    task_cols = _columns("tasks")
    if "seed_vulnerability_task_id" not in task_cols:
        op.add_column(
            "tasks",
            sa.Column("seed_vulnerability_task_id", sa.String(), nullable=True),
            schema=AXIOM_SCHEMA,
        )
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(
            "tasks", schema=AXIOM_SCHEMA
        )
    }
    if _FK_NAME not in constraints:
        op.create_foreign_key(
            _FK_NAME,
            "tasks",
            "tasks",
            ["seed_vulnerability_task_id"],
            ["id"],
            ondelete="SET NULL",
            source_schema=AXIOM_SCHEMA,
            referent_schema=AXIOM_SCHEMA,
        )


def downgrade() -> None:
    constraints = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys(
            "tasks", schema=AXIOM_SCHEMA
        )
    }
    if _FK_NAME in constraints:
        op.drop_constraint(_FK_NAME, "tasks", schema=AXIOM_SCHEMA, type_="foreignkey")
    task_cols = _columns("tasks")
    if "seed_vulnerability_task_id" in task_cols:
        op.drop_column("tasks", "seed_vulnerability_task_id", schema=AXIOM_SCHEMA)
    vuln_cols = _columns("vulnerabilities")
    for name, _column in reversed(_VULN_ADD):
        if name in vuln_cols:
            op.drop_column("vulnerabilities", name, schema=AXIOM_SCHEMA)
