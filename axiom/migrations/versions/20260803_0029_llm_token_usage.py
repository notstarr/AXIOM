"""Persist Pi Session token usage and source completeness."""

from alembic import op
import sqlalchemy as sa


revision = "20260803_0029"
down_revision = "20260729_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_token_usage",
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=True),
        sa.Column("reason_run_id", sa.String(), nullable=True),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("worker", sa.String(), nullable=False, server_default=""),
        sa.Column("provider", sa.String(), nullable=False, server_default=""),
        sa.Column("model", sa.String(), nullable=False, server_default=""),
        sa.Column("requested_model", sa.String(), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(), nullable=False, server_default=""),
        sa.Column("session_file", sa.Text(), nullable=False),
        sa.Column("session_entry_id", sa.String(), nullable=False),
        sa.Column("usage_kind", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reported_total_tokens", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "phase IN ('bootstrap','explore','reason','unknown')",
            name="ck_llm_token_usage_phase",
        ),
        sa.CheckConstraint(
            "usage_kind IN ('assistant','compaction','branch_summary','tool_result','legacy_audit')",
            name="ck_llm_token_usage_usage_kind",
        ),
        sa.CheckConstraint(
            "source IN ('live','backfill','legacy')",
            name="ck_llm_token_usage_source",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 "
            "AND output_tokens >= 0 AND reasoning_tokens >= 0 "
            "AND (reported_total_tokens IS NULL OR reported_total_tokens >= 0)",
            name="ck_llm_token_usage_nonnegative_tokens",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_key", name="pk_llm_token_usage"),
    )
    op.create_index(
        "ix_llm_token_usage_task_occurred",
        "llm_token_usage",
        ["task_id", "occurred_at"],
    )
    op.create_index(
        "ix_llm_token_usage_model_occurred",
        "llm_token_usage",
        ["provider", "model", "occurred_at"],
    )
    op.create_index(
        "ix_llm_token_usage_task_phase_worker",
        "llm_token_usage",
        ["task_id", "phase", "worker"],
    )

    op.create_table(
        "llm_token_usage_sources",
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=True),
        sa.Column("reason_run_id", sa.String(), nullable=True),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("session_file", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("scanned_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "status IN ('complete','incomplete')",
            name="ck_llm_token_usage_sources_status",
        ),
        sa.CheckConstraint(
            "source IN ('live','backfill','legacy')",
            name="ck_llm_token_usage_sources_source",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_key", name="pk_llm_token_usage_sources"),
    )
    op.create_index(
        "ix_llm_token_usage_sources_task_status",
        "llm_token_usage_sources",
        ["task_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_token_usage_sources_task_status", table_name="llm_token_usage_sources")
    op.drop_table("llm_token_usage_sources")
    op.drop_index("ix_llm_token_usage_task_phase_worker", table_name="llm_token_usage")
    op.drop_index("ix_llm_token_usage_model_occurred", table_name="llm_token_usage")
    op.drop_index("ix_llm_token_usage_task_occurred", table_name="llm_token_usage")
    op.drop_table("llm_token_usage")
