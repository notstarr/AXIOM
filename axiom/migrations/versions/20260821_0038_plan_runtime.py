"""Create independent PlanRun and PlanWorkspace persistence tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa



revision = "20260821_0038"
down_revision = "20260820_0037"
branch_labels = None
depends_on = None

_ACTIVE = "status IN ('claimed','running','concluding')"


def _copy_legacy_plan_rows(bind) -> None:
    from axiom.migration.plan_legacy import migrate_legacy_plan_rows

    migrate_legacy_plan_rows(bind)


def upgrade() -> None:
    audit_columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("audit_events")}
    if "plan_run_id" not in audit_columns:
        with op.batch_alter_table("audit_events") as batch:
            batch.add_column(sa.Column("plan_run_id", sa.String(), nullable=True))
    existing_context_columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("context_revisions")}
    if "plan_run_id" not in existing_context_columns:
        with op.batch_alter_table("context_revisions") as batch:
            batch.add_column(sa.Column("plan_run_id", sa.String(), nullable=True))
            batch.create_unique_constraint(
                "uq_context_revisions_task_id_plan_run_id_lease_id",
                ["task_id", "plan_run_id", "lease_id"],
            )
            batch.create_index(
                "ix_context_revisions_task_plan_run",
                ["task_id", "plan_run_id"],
                unique=False,
            )
    # The independent Plan owner is also a first-class token-usage owner.
    # Older SQLite/PostgreSQL schemas only know about Intent, Reason and (on
    # newer installations) Review.  Add the nullable owner column and extend
    # the phase check idempotently so diagnostics can read the new metadata
    # immediately after this migration.
    token_columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("llm_token_usage")}
    if "plan_run_id" not in token_columns:
        with op.batch_alter_table("llm_token_usage") as batch:
            batch.add_column(sa.Column("plan_run_id", sa.String(), nullable=True))
            for constraint in sa.inspect(op.get_bind()).get_check_constraints("llm_token_usage"):
                if constraint.get("name") and "phase IN" in str(constraint.get("sqltext", "")):
                    batch.drop_constraint(constraint["name"], type="check")
            batch.create_check_constraint(
                "ck_llm_token_usage_phase",
                "phase IN ('bootstrap','explore','reason','plan','review','unknown')",
            )
    source_columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("llm_token_usage_sources")}
    if "plan_run_id" not in source_columns:
        with op.batch_alter_table("llm_token_usage_sources") as batch:
            batch.add_column(sa.Column("plan_run_id", sa.String(), nullable=True))
    op.create_table(
        "plan_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("goal_cycle_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("trigger_payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("worker_pool_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("worker", sa.String(), nullable=True),
        sa.Column("settings_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("execute_timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("conclude_timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("lease_id", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.String(), nullable=True),
        sa.Column("last_heartbeat_at", sa.String(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("infrastructure_retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.String(), nullable=True),
        sa.Column("last_failure_kind", sa.String(), nullable=False, server_default=""),
        sa.Column("failure_class", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_kind", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("activity_tool", sa.String(), nullable=False, server_default=""),
        sa.Column("activity_started_at", sa.String(), nullable=True),
        sa.Column("activity_updated_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("concluded_at", sa.String(), nullable=True),
        sa.Column("decision_json", sa.Text(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("id", name="pk_plan_runs"),
        sa.UniqueConstraint("task_id", "id", name="uq_plan_runs_task_id_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('pending','claimed','running','concluding','completed','failed','cancelled')", name="ck_plan_runs_status"),
    )
    op.create_index("ix_plan_runs_task_status", "plan_runs", ["task_id", "status"])
    op.create_index("ix_plan_runs_retry", "plan_runs", ["status", "next_retry_at"])
    op.create_index(
        "uq_plan_runs_one_in_flight_per_task",
        "plan_runs",
        ["task_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE),
        postgresql_where=sa.text(_ACTIVE),
    )

    op.create_table(
        "plan_run_sources",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("plan_run_id", sa.String(), nullable=False),
        sa.Column("fact_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["task_id", "plan_run_id"], ["plan_runs.task_id", "plan_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id", "fact_id"], ["facts.task_id", "facts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("plan_run_id", "fact_id", name="uq_plan_run_sources_plan_run_id_fact_id"),
    )
    op.create_index("ix_plan_run_sources_task_fact", "plan_run_sources", ["task_id", "fact_id"])

    op.create_table(
        "plan_workspaces",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("plan_run_id", sa.String(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("context_revision", sa.Integer(), nullable=False),
        sa.Column("draft_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("worker", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("decision_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("committed_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_plan_workspaces"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id", "plan_run_id"], ["plan_runs.task_id", "plan_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "plan_run_id", "lease_id", name="uq_plan_workspaces_task_run_lease"),
        sa.CheckConstraint("status IN ('open','committed','abandoned')", name="ck_plan_workspaces_status"),
    )
    op.create_index("ix_plan_workspaces_task_status", "plan_workspaces", ["task_id", "status"])
    _copy_legacy_plan_rows(op.get_bind())


def downgrade() -> None:
    if "plan_run_id" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("llm_token_usage_sources")}:
        with op.batch_alter_table("llm_token_usage_sources") as batch:
            batch.drop_column("plan_run_id")
    if "plan_run_id" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("llm_token_usage")}:
        with op.batch_alter_table("llm_token_usage") as batch:
            for constraint in sa.inspect(op.get_bind()).get_check_constraints("llm_token_usage"):
                if constraint.get("name") and "phase IN" in str(constraint.get("sqltext", "")):
                    batch.drop_constraint(constraint["name"], type="check")
            batch.create_check_constraint(
                "ck_llm_token_usage_phase",
                "phase IN ('bootstrap','explore','reason','review','unknown')",
            )
            batch.drop_column("plan_run_id")
    from axiom.migration.plan_legacy import restore_plan_rows_for_downgrade

    restore_plan_rows_for_downgrade(op.get_bind())
    if "plan_run_id" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("audit_events")}:
        with op.batch_alter_table("audit_events") as batch:
            batch.drop_column("plan_run_id")
    if "plan_run_id" in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("context_revisions")}:
        with op.batch_alter_table("context_revisions") as batch:
            batch.drop_index("ix_context_revisions_task_plan_run")
            batch.drop_constraint(
                "uq_context_revisions_task_id_plan_run_id_lease_id",
                type_="unique",
            )
            batch.drop_column("plan_run_id")
    op.drop_index("ix_plan_workspaces_task_status", table_name="plan_workspaces")
    op.drop_table("plan_workspaces")
    op.drop_index("ix_plan_run_sources_task_fact", table_name="plan_run_sources")
    op.drop_table("plan_run_sources")
    op.drop_index("uq_plan_runs_one_in_flight_per_task", table_name="plan_runs")
    op.drop_index("ix_plan_runs_retry", table_name="plan_runs")
    op.drop_index("ix_plan_runs_task_status", table_name="plan_runs")
    op.drop_table("plan_runs")
