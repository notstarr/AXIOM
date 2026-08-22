from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, Table, Text

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import UTCDateTimeText, metadata


llm_token_usage = Table(
    "llm_token_usage",
    metadata,
    Column("source_key", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("plan_run_id", String),
    Column("review_run_id", String),
    Column("lease_id", String, nullable=False),
    Column("phase", String, nullable=False),
    Column("worker", String, nullable=False, default=""),
    Column("provider", String, nullable=False, default=""),
    Column("model", String, nullable=False, default=""),
    Column("requested_model", String, nullable=False, default=""),
    Column("session_id", String, nullable=False, default=""),
    Column("session_file", Text, nullable=False),
    Column("session_entry_id", String, nullable=False),
    Column("usage_kind", String, nullable=False),
    Column("occurred_at", UTCDateTimeText, nullable=False),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("cache_read_tokens", Integer, nullable=False, default=0),
    Column("cache_write_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("reasoning_tokens", Integer, nullable=False, default=0),
    Column("reported_total_tokens", Integer),
    Column("source", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    CheckConstraint(
        "phase IN ('bootstrap','explore','reason','plan','review','unknown')",
        name="phase",
    ),
    CheckConstraint(
        "usage_kind IN ('assistant','compaction','branch_summary','tool_result','legacy_audit')",
        name="usage_kind",
    ),
    CheckConstraint("source IN ('live','backfill','legacy')", name="source"),
    CheckConstraint(
        "input_tokens >= 0 AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 "
        "AND output_tokens >= 0 AND reasoning_tokens >= 0 "
        "AND (reported_total_tokens IS NULL OR reported_total_tokens >= 0)",
        name="nonnegative_tokens",
    ),
)
Index("ix_llm_token_usage_task_occurred", llm_token_usage.c.task_id, llm_token_usage.c.occurred_at)
Index(
    "ix_llm_token_usage_model_occurred",
    llm_token_usage.c.provider,
    llm_token_usage.c.model,
    llm_token_usage.c.occurred_at,
)
Index(
    "ix_llm_token_usage_task_phase_worker",
    llm_token_usage.c.task_id,
    llm_token_usage.c.phase,
    llm_token_usage.c.worker,
)


llm_token_usage_sources = Table(
    "llm_token_usage_sources",
    metadata,
    Column("source_key", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("plan_run_id", String),
    Column("review_run_id", String),
    Column("lease_id", String, nullable=False),
    Column("session_file", Text, nullable=False),
    Column("session_id", String, nullable=False, default=""),
    Column("status", String, nullable=False),
    Column("error", Text, nullable=False, default=""),
    Column("source", String, nullable=False),
    Column("scanned_at", UTCDateTimeText, nullable=False),
    CheckConstraint("status IN ('complete','incomplete')", name="status"),
    CheckConstraint("source IN ('live','backfill','legacy')", name="source"),
)
Index(
    "ix_llm_token_usage_sources_task_status",
    llm_token_usage_sources.c.task_id,
    llm_token_usage_sources.c.status,
)
