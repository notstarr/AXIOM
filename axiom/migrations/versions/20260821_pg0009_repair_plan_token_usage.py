"""Repair Plan token-usage columns on existing PostgreSQL installations."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260821_pg0009"
down_revision = "20260821_pg0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    token_columns = {
        item["name"]
        for item in inspector.get_columns("llm_token_usage", schema=AXIOM_SCHEMA)
    }
    if "plan_run_id" not in token_columns:
        op.add_column(
            "llm_token_usage",
            sa.Column("plan_run_id", sa.String(), nullable=True),
            schema=AXIOM_SCHEMA,
        )
    source_columns = {
        item["name"]
        for item in inspector.get_columns("llm_token_usage_sources", schema=AXIOM_SCHEMA)
    }
    if "plan_run_id" not in source_columns:
        op.add_column(
            "llm_token_usage_sources",
            sa.Column("plan_run_id", sa.String(), nullable=True),
            schema=AXIOM_SCHEMA,
        )
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."llm_token_usage" '
            'DROP CONSTRAINT IF EXISTS "ck_llm_token_usage_phase"'
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."llm_token_usage" '
            'ADD CONSTRAINT "ck_llm_token_usage_phase" CHECK '
            "(phase IN ('bootstrap','explore','reason','plan','review','unknown'))"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."llm_token_usage" '
            'DROP CONSTRAINT IF EXISTS "ck_llm_token_usage_phase"'
        )
    )
    op.execute(
        sa.text(
            f'ALTER TABLE "{AXIOM_SCHEMA}"."llm_token_usage" '
            'ADD CONSTRAINT "ck_llm_token_usage_phase" CHECK '
            "(phase IN ('bootstrap','explore','reason','review','unknown'))"
        )
    )
    if "plan_run_id" in {
        item["name"]
        for item in inspector.get_columns("llm_token_usage_sources", schema=AXIOM_SCHEMA)
    }:
        op.drop_column("llm_token_usage_sources", "plan_run_id", schema=AXIOM_SCHEMA)
    if "plan_run_id" in {
        item["name"]
        for item in inspector.get_columns("llm_token_usage", schema=AXIOM_SCHEMA)
    }:
        op.drop_column("llm_token_usage", "plan_run_id", schema=AXIOM_SCHEMA)
