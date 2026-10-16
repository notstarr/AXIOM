"""Bind red-team Intents to Chains and align Chain status with Todo."""
import json

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA

revision = "20261015_pg0022"
down_revision = "20261014_pg0021"
branch_labels = None
depends_on = None

_STATUS_MAP = {
    "planned": "open",
    "active": "partial",
    "achieved": "completed",
    "failed": "abandoned",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    schema = AXIOM_SCHEMA
    if inspector.has_table("intents", schema=schema):
        cols = {c["name"] for c in inspector.get_columns("intents", schema=schema)}
        if "chain_id" not in cols:
            op.add_column("intents", sa.Column("chain_id", sa.String(), nullable=True), schema=schema)
    if inspector.has_table("red_team_chains", schema=schema):
        cols = {c["name"] for c in inspector.get_columns("red_team_chains", schema=schema)}
        if "status_reason" not in cols:
            op.add_column(
                "red_team_chains",
                sa.Column("status_reason", sa.Text(), nullable=False, server_default=""),
                schema=schema,
            )
        for old, new in _STATUS_MAP.items():
            op.execute(
                sa.text(
                    f'UPDATE "{schema}".red_team_chains SET status = :new WHERE status = :old'
                ).bindparams(old=old, new=new)
            )
    if inspector.has_table("intents", schema=schema):
        rows = bind.execute(
            sa.text(
                f'SELECT task_id, id, plan_metadata_json FROM "{schema}".intents '
                "WHERE chain_id IS NULL AND plan_metadata_json IS NOT NULL"
            )
        ).mappings()
        for row in rows:
            raw = row["plan_metadata_json"]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    continue
            if not isinstance(raw, dict):
                continue
            chain_id = raw.get("chain_id")
            if not isinstance(chain_id, str) or not chain_id.strip():
                continue
            bind.execute(
                sa.text(
                    f'UPDATE "{schema}".intents SET chain_id = :chain_id '
                    "WHERE task_id = :task_id AND id = :id"
                ).bindparams(chain_id=chain_id.strip(), task_id=row["task_id"], id=row["id"])
            )


def downgrade() -> None:
    pass
