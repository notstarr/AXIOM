from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from axiom.modules.graph.tables import facts, intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


reason_runs = Table(
    "reason_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("goal_cycle_id", String),
    Column("status", String, nullable=False, default="pending"),
    Column("trigger", String, nullable=False),
    Column("trigger_payload_json", Text, nullable=False, default="{}"),
    Column("input_fingerprint", String, nullable=False),
    Column("worker_pool_json", Text, nullable=False, default="[]"),
    Column("worker", String),
    Column("settings_version", Integer, nullable=False),
    Column("execute_timeout_seconds", Integer, nullable=False),
    Column("conclude_timeout_seconds", Integer, nullable=False),
    Column("target_open_intents", Integer, nullable=False),
    Column("max_new_intents", Integer, nullable=False),
    Column("explore_execute_min_seconds", Integer, nullable=False),
    Column("explore_execute_max_seconds", Integer, nullable=False),
    Column("explore_conclude_min_seconds", Integer, nullable=False),
    Column("explore_conclude_max_seconds", Integer, nullable=False),
    Column("lease_id", String),
    Column("claimed_at", String),
    Column("last_heartbeat_at", String),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("infrastructure_retry_count", Integer, nullable=False, default=0),
    Column("next_retry_at", String),
    Column("last_failure_kind", String, nullable=False, default=""),
    Column("failure_class", String, nullable=False, default=""),
    Column("activity_kind", String, nullable=False, default=""),
    Column("activity_summary", Text, nullable=False, default=""),
    Column("activity_tool", String, nullable=False, default=""),
    Column("activity_started_at", String),
    Column("activity_updated_at", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("concluded_at", String),
    CheckConstraint(
        "status IN ('pending','claimed','running','concluding','completed','failed','cancelled')",
        name="status",
    ),
    UniqueConstraint("task_id", "id"),
)
Index("ix_reason_runs_task_status", reason_runs.c.task_id, reason_runs.c.status)
Index("ix_reason_runs_retry", reason_runs.c.status, reason_runs.c.next_retry_at)
Index(
    "uq_reason_runs_one_open_per_task",
    reason_runs.c.task_id,
    unique=True,
    sqlite_where=reason_runs.c.status.in_(["pending", "claimed", "running", "concluding"]),
)


reason_run_sources = Table(
    "reason_run_sources",
    metadata,
    Column("task_id", String, nullable=False),
    Column("reason_run_id", String, nullable=False),
    Column("fact_id", String, nullable=False),
    ForeignKeyConstraint(
        ["task_id", "reason_run_id"],
        [reason_runs.c.task_id, reason_runs.c.id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["task_id", "fact_id"],
        [facts.c.task_id, facts.c.id],
        ondelete="CASCADE",
    ),
    UniqueConstraint("reason_run_id", "fact_id"),
)
Index("ix_reason_run_sources_task_fact", reason_run_sources.c.task_id, reason_run_sources.c.fact_id)


reason_workspaces = Table(
    "reason_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("lease_id", String, nullable=False),
    Column("status", String, nullable=False, default="open"),
    Column("context_revision", Integer, nullable=False),
    Column("draft_json", Text, nullable=False, default="{}"),
    Column("worker", String, nullable=False),
    Column("failure_reason", Text, nullable=False, default=""),
    Column("result_json", Text, nullable=False, default="{}"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("committed_at", String),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["task_id", "reason_run_id"],
        [reason_runs.c.task_id, reason_runs.c.id],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "(intent_id IS NOT NULL AND reason_run_id IS NULL) OR "
        "(intent_id IS NULL AND reason_run_id IS NOT NULL)",
        name="owner",
    ),
    CheckConstraint("status IN ('open','committed','abandoned')", name="status"),
    UniqueConstraint("task_id", "intent_id", "lease_id"),
    UniqueConstraint("task_id", "reason_run_id", "lease_id"),
)
Index("ix_reason_workspaces_task_status", reason_workspaces.c.task_id, reason_workspaces.c.status)
