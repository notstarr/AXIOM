from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


facts = Table(
    "facts",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("description", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
)


intents = Table(
    "intents",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("todo_id", String),
    Column("kind", String, nullable=False, default="explore"),
    Column("description", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("creator", String, nullable=False),
    Column("worker", String),
    Column("worker_pool_json", Text, nullable=False, default="[]"),
    Column("targets_json", Text, nullable=False, default="[]"),
    Column("execute_timeout_seconds", Integer),
    Column("conclude_timeout_seconds", Integer),
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
    Column("to_fact_id", String),
    Column("created_at", String, nullable=False),
    Column("concluded_at", String),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "to_fact_id"],
        [facts.c.task_id, facts.c.id],
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "status IN ('pending','claimed','running','concluding','completed','failed','cancelled')",
        name="status",
    ),
    CheckConstraint("kind IN ('bootstrap','reason','explore')", name="kind"),
)


intent_sources = Table(
    "intent_sources",
    metadata,
    Column("task_id", String, nullable=False),
    Column("intent_id", String, nullable=False),
    Column("fact_id", String, nullable=False),
    PrimaryKeyConstraint("task_id", "intent_id", "fact_id"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["task_id", "fact_id"],
        [facts.c.task_id, facts.c.id],
        ondelete="RESTRICT",
    ),
)
