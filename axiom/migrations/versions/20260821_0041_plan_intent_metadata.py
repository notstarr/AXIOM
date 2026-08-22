"""Persist Plan Intent provenance and deduplication metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260821_0041"
down_revision = "20260821_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("intents")}
    with op.batch_alter_table("intents") as batch:
        if "dedup_fingerprint" not in columns:
            batch.add_column(
                sa.Column(
                    "dedup_fingerprint",
                    sa.String(),
                    nullable=False,
                    server_default="",
                )
            )
        if "plan_metadata_json" not in columns:
            batch.add_column(
                sa.Column(
                    "plan_metadata_json",
                    sa.Text(),
                    nullable=False,
                    server_default="{}",
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("intents")}
    with op.batch_alter_table("intents") as batch:
        if "plan_metadata_json" in columns:
            batch.drop_column("plan_metadata_json")
        if "dedup_fingerprint" in columns:
            batch.drop_column("dedup_fingerprint")
