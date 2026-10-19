from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


c2_listeners = Table(
    "c2_listeners",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("kind", String, nullable=False, default="http"),
    Column("host", String, nullable=False, default="0.0.0.0"),
    Column("port", Integer, nullable=False),
    Column("status", String, nullable=False, default="stopped"),
    Column("error", Text, nullable=False, default=""),
    Column("created_by", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
)
Index("ix_c2_listeners_task_status", c2_listeners.c.task_id, c2_listeners.c.status)


c2_sessions = Table(
    "c2_sessions",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("listener_id", String, nullable=False),
    Column("kind", String, nullable=False, default="http"),
    Column("remote", String, nullable=False, default=""),
    Column("info_json", JSONText, nullable=False, default="{}"),
    Column("status", String, nullable=False, default="active"),
    Column("sleep_seconds", Integer, nullable=False, default=5),
    Column("last_seen_at", UTCDateTimeText),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "listener_id"],
        [c2_listeners.c.task_id, c2_listeners.c.id],
        ondelete="CASCADE",
    ),
)
Index("ix_c2_sessions_task_listener", c2_sessions.c.task_id, c2_sessions.c.listener_id)


c2_tasks = Table(
    "c2_tasks",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("session_id", String, nullable=False),
    Column("command", Text, nullable=False),
    Column("status", String, nullable=False, default="queued"),
    Column("output", Text, nullable=False, default=""),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("actor", String, nullable=False),
    Column("intent_id", String),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "session_id"],
        [c2_sessions.c.task_id, c2_sessions.c.id],
        ondelete="CASCADE",
    ),
)
Index("ix_c2_tasks_session_status", c2_tasks.c.task_id, c2_tasks.c.session_id, c2_tasks.c.status)
