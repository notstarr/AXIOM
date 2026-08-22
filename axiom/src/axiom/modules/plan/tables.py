from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Table, Text, UniqueConstraint

from axiom.modules.graph.tables import facts
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


plan_runs = Table(
    "plan_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("goal_cycle_id", String),
    Column("status", String, nullable=False, default="pending"),
    Column("trigger", String, nullable=False, default="reconcile"),
    Column("trigger_payload_json", JSONText, nullable=False, default="{}"),
    Column("input_fingerprint", String, nullable=False),
    Column("worker_pool_json", JSONText, nullable=False, default="[]"),
    Column("worker", String),
    Column("settings_version", Integer, nullable=False, default=1),
    Column("execute_timeout_seconds", Integer, nullable=False, default=300),
    Column("conclude_timeout_seconds", Integer, nullable=False, default=120),
    Column("lease_id", String),
    Column("claimed_at", UTCDateTimeText),
    Column("last_heartbeat_at", UTCDateTimeText),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("infrastructure_retry_count", Integer, nullable=False, default=0),
    Column("next_retry_at", UTCDateTimeText),
    Column("last_failure_kind", String, nullable=False, default=""),
    Column("failure_class", String, nullable=False, default=""),
    Column("activity_kind", String, nullable=False, default=""),
    Column("activity_summary", Text, nullable=False, default=""),
    Column("activity_tool", String, nullable=False, default=""),
    Column("activity_started_at", UTCDateTimeText),
    Column("activity_updated_at", UTCDateTimeText),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("concluded_at", UTCDateTimeText),
    Column("decision_json", JSONText, nullable=False, default="{}"),
    CheckConstraint(
        "status IN ('pending','claimed','running','concluding','completed','failed','cancelled')",
        name="status",
    ),
    UniqueConstraint("task_id", "id"),
)
Index("ix_plan_runs_task_status", plan_runs.c.task_id, plan_runs.c.status)
Index("ix_plan_runs_retry", plan_runs.c.status, plan_runs.c.next_retry_at)
Index(
    "uq_plan_runs_one_in_flight_per_task",
    plan_runs.c.task_id,
    unique=True,
    sqlite_where=plan_runs.c.status.in_(["claimed", "running", "concluding"]),
    postgresql_where=plan_runs.c.status.in_(["claimed", "running", "concluding"]),
)


plan_run_sources = Table(
    "plan_run_sources",
    metadata,
    Column("task_id", String, nullable=False),
    Column("plan_run_id", String, nullable=False),
    Column("fact_id", String, nullable=False),
    ForeignKeyConstraint(
        ["task_id", "plan_run_id"],
        [plan_runs.c.task_id, plan_runs.c.id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["task_id", "fact_id"],
        [facts.c.task_id, facts.c.id],
        ondelete="CASCADE",
    ),
    UniqueConstraint("plan_run_id", "fact_id"),
)
Index("ix_plan_run_sources_task_fact", plan_run_sources.c.task_id, plan_run_sources.c.fact_id)


plan_workspaces = Table(
    "plan_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("plan_run_id", String, nullable=False),
    Column("lease_id", String, nullable=False),
    Column("status", String, nullable=False, default="open"),
    Column("context_revision", Integer, nullable=False),
    Column("draft_json", JSONText, nullable=False, default="{}"),
    Column("worker", String, nullable=False),
    Column("failure_reason", Text, nullable=False, default=""),
    Column("result_json", JSONText, nullable=False, default="{}"),
    Column("decision_json", JSONText, nullable=False, default="{}"),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("committed_at", UTCDateTimeText),
    ForeignKeyConstraint(
        ["task_id", "plan_run_id"],
        [plan_runs.c.task_id, plan_runs.c.id],
        ondelete="CASCADE",
    ),
    CheckConstraint("status IN ('open','committed','abandoned')", name="status"),
    UniqueConstraint("task_id", "plan_run_id", "lease_id"),
)
Index("ix_plan_workspaces_task_status", plan_workspaces.c.task_id, plan_workspaces.c.status)
