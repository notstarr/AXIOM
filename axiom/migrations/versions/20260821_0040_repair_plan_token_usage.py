"""Repair Plan token-usage columns on installations upgraded before 0038 was amended."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260821_0040"
down_revision = "20260821_0039"
branch_labels = None
depends_on = None


def _check_names(bind: sa.Connection) -> list[str]:
    return [
        str(item["name"])
        for item in sa.inspect(bind).get_check_constraints("llm_token_usage")
        if item.get("name") and "phase IN" in str(item.get("sqltext", ""))
    ]


def upgrade() -> None:
    bind = op.get_bind()
    token_columns = {item["name"] for item in sa.inspect(bind).get_columns("llm_token_usage")}
    if "plan_run_id" not in token_columns:
        with op.batch_alter_table("llm_token_usage") as batch:
            batch.add_column(sa.Column("plan_run_id", sa.String(), nullable=True))
    # Some installations already had the nullable owner column from an
    # earlier partial 0038 upgrade, but retained the old phase CHECK.  Repair
    # the constraint independently of whether the column was added above.
    with op.batch_alter_table("llm_token_usage") as batch:
        for name in _check_names(bind):
            batch.drop_constraint(name, type="check")
        batch.create_check_constraint(
            "ck_llm_token_usage_phase",
            "phase IN ('bootstrap','explore','reason','plan','review','unknown')",
        )
    source_columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("llm_token_usage_sources")
    }
    if "plan_run_id" not in source_columns:
        with op.batch_alter_table("llm_token_usage_sources") as batch:
            batch.add_column(sa.Column("plan_run_id", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    source_columns = {
        item["name"]
        for item in sa.inspect(bind).get_columns("llm_token_usage_sources")
    }
    if "plan_run_id" in source_columns:
        with op.batch_alter_table("llm_token_usage_sources") as batch:
            batch.drop_column("plan_run_id")
    token_columns = {item["name"] for item in sa.inspect(bind).get_columns("llm_token_usage")}
    if "plan_run_id" in token_columns:
        with op.batch_alter_table("llm_token_usage") as batch:
            for name in _check_names(bind):
                batch.drop_constraint(name, type="check")
            batch.create_check_constraint(
                "ck_llm_token_usage_phase",
                "phase IN ('bootstrap','explore','reason','review','unknown')",
            )
            batch.drop_column("plan_run_id")
