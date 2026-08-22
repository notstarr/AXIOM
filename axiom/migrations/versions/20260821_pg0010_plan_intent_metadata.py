"""Persist Plan Intent provenance and deduplication metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260821_pg0010"
down_revision = "20260821_pg0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("intents", schema=AXIOM_SCHEMA)
    }
    if "dedup_fingerprint" not in columns:
        op.add_column(
            "intents",
            sa.Column(
                "dedup_fingerprint",
                sa.String(),
                nullable=False,
                server_default="",
            ),
            schema=AXIOM_SCHEMA,
        )
    if "plan_metadata_json" not in columns:
        op.add_column(
            "intents",
            sa.Column(
                "plan_metadata_json",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            schema=AXIOM_SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("intents", schema=AXIOM_SCHEMA)
    }
    if "plan_metadata_json" in columns:
        op.drop_column("intents", "plan_metadata_json", schema=AXIOM_SCHEMA)
    if "dedup_fingerprint" in columns:
        op.drop_column("intents", "dedup_fingerprint", schema=AXIOM_SCHEMA)
