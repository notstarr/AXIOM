from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    false,
)

from axiom.modules.projects.tables import projects
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, ForeignKey(projects.c.id, ondelete="RESTRICT"), nullable=False),
    Column("title", Text, nullable=False),
    Column("origin_host", String),
    Column("origin_scheme", String),
    Column("origin_port", String),
    Column("origin_input", Text, nullable=False),
    Column("goal", Text, nullable=False),
    Column("current_goal_cycle_id", String),
    Column("status", String, nullable=False),
    Column("phase", String, nullable=False),
    Column("scope_json", JSONText, nullable=False),
    Column("worker_pool_json", JSONText, nullable=False),
    Column("guard_enabled", Boolean, nullable=False, default=False),
    Column("external_report_enabled", Boolean, nullable=False, default=False),
    Column("legacy_readonly", Boolean, nullable=False, default=False),
    Column("archived", Boolean, nullable=False, server_default=false()),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("completed_at", UTCDateTimeText),
    Column("run_duration_seconds", Integer),
    Column("run_started_at", UTCDateTimeText),
    Column("run_deadline_at", UTCDateTimeText),
    Column("last_pause_reason", String),
    CheckConstraint("status IN ('running','stopped','completed','failed')", name="status"),
    CheckConstraint("phase IN ('bootstrap','reason','explore','waiting','none')", name="phase"),
    CheckConstraint(
        "run_duration_seconds IS NULL OR "
        "(run_duration_seconds BETWEEN 60 AND 31536000 AND run_duration_seconds % 60 = 0)",
        name="run_duration_seconds",
    ),
    CheckConstraint(
        "last_pause_reason IS NULL OR last_pause_reason IN ('manual','run_deadline_reached')",
        name="last_pause_reason",
    ),
)

Index("ix_tasks_status_run_deadline", tasks.c.status, tasks.c.run_deadline_at)
Index("ix_tasks_archived_status", tasks.c.archived, tasks.c.status)


task_hints = Table(
    "task_hints",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("content", Text, nullable=False),
    Column("creator", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
)
