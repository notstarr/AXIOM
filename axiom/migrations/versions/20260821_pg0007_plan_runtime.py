"""Create independent PlanRun and PlanWorkspace persistence tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20260821_pg0007"
down_revision = "20260820_pg0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "plan_run_id" not in {item["name"] for item in inspector.get_columns("audit_events", schema=AXIOM_SCHEMA)}:
        op.add_column("audit_events", sa.Column("plan_run_id", sa.String(), nullable=True), schema=AXIOM_SCHEMA)
    if "plan_run_id" not in {item["name"] for item in inspector.get_columns("context_revisions", schema=AXIOM_SCHEMA)}:
        op.add_column("context_revisions", sa.Column("plan_run_id", sa.String(), nullable=True), schema=AXIOM_SCHEMA)
        op.create_unique_constraint(
            "uq_context_revisions_task_id_plan_run_id_lease_id",
            "context_revisions",
            ["task_id", "plan_run_id", "lease_id"],
            schema=AXIOM_SCHEMA,
        )
        op.create_index("ix_context_revisions_task_plan_run", "context_revisions", ["task_id", "plan_run_id"], schema=AXIOM_SCHEMA)
    token_columns = {item["name"] for item in inspector.get_columns("llm_token_usage", schema=AXIOM_SCHEMA)}
    if "plan_run_id" not in token_columns:
        op.add_column("llm_token_usage", sa.Column("plan_run_id", sa.String(), nullable=True), schema=AXIOM_SCHEMA)
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
    source_columns = {item["name"] for item in inspector.get_columns("llm_token_usage_sources", schema=AXIOM_SCHEMA)}
    if "plan_run_id" not in source_columns:
        op.add_column("llm_token_usage_sources", sa.Column("plan_run_id", sa.String(), nullable=True), schema=AXIOM_SCHEMA)
    # PostgreSQL baseline metadata already contains the tables.  Explicitly
    # create them for databases upgraded from pg0006, where they do not yet
    # exist, while keeping native JSONB/timestamptz types through metadata.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("plan_runs", schema=AXIOM_SCHEMA):
        from axiom.modules.plan.tables import plan_run_sources, plan_runs, plan_workspaces
        plan_runs.create(bind)
        plan_run_sources.create(bind)
        plan_workspaces.create(bind)
    from axiom.migration.plan_legacy import migrate_legacy_plan_rows

    migrate_legacy_plan_rows(bind, schema=AXIOM_SCHEMA)


def downgrade() -> None:
    from axiom.migration.plan_legacy import restore_plan_rows_for_downgrade

    restore_plan_rows_for_downgrade(op.get_bind(), schema=AXIOM_SCHEMA)
    from axiom.modules.plan.tables import plan_run_sources, plan_runs, plan_workspaces
    bind = op.get_bind()
    plan_workspaces.drop(bind, checkfirst=True)
    plan_run_sources.drop(bind, checkfirst=True)
    plan_runs.drop(bind, checkfirst=True)
    inspector = sa.inspect(bind)
    if "plan_run_id" in {item["name"] for item in inspector.get_columns("audit_events", schema=AXIOM_SCHEMA)}:
        op.drop_column("audit_events", "plan_run_id", schema=AXIOM_SCHEMA)
    if "plan_run_id" in {item["name"] for item in inspector.get_columns("context_revisions", schema=AXIOM_SCHEMA)}:
        op.drop_index("ix_context_revisions_task_plan_run", table_name="context_revisions", schema=AXIOM_SCHEMA)
        op.drop_constraint(
            "uq_context_revisions_task_id_plan_run_id_lease_id",
            "context_revisions",
            type_="unique",
            schema=AXIOM_SCHEMA,
        )
        op.drop_column("context_revisions", "plan_run_id", schema=AXIOM_SCHEMA)
    inspector = sa.inspect(bind)
    if "plan_run_id" in {item["name"] for item in inspector.get_columns("llm_token_usage_sources", schema=AXIOM_SCHEMA)}:
        op.drop_column("llm_token_usage_sources", "plan_run_id", schema=AXIOM_SCHEMA)
    if "plan_run_id" in {item["name"] for item in inspector.get_columns("llm_token_usage", schema=AXIOM_SCHEMA)}:
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
        op.drop_column("llm_token_usage", "plan_run_id", schema=AXIOM_SCHEMA)
