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
    false,
)

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


facts = Table(
    "facts",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("description", Text, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
)


intents = Table(
    "intents",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("goal_cycle_id", String),
    Column("todo_id", String),
    Column("kind", String, nullable=False, default="explore"),
    Column("description", Text, nullable=False),
    Column("priority", Integer, nullable=False, default=50, server_default="50"),
    Column("status", String, nullable=False),
    Column("creator", String, nullable=False),
    Column("worker", String),
    Column("worker_pool_json", JSONText, nullable=False, default="[]"),
    Column("targets_json", JSONText, nullable=False, default="[]"),
    Column("execute_timeout_seconds", Integer),
    Column("conclude_timeout_seconds", Integer),
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
    Column("to_fact_id", String),
    Column(
        "human_intervention_required",
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    ),
    Column(
        "human_intervention_reason",
        Text,
        nullable=False,
        default="",
        server_default="",
    ),
    Column("human_intervention_marked_at", UTCDateTimeText),
    Column(
        "assessment_status",
        String,
        nullable=False,
        default="open",
        server_default="open",
    ),
    Column(
        "assessment_reason",
        Text,
        nullable=False,
        default="",
        server_default="",
    ),
    Column("assessed_at", UTCDateTimeText),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("concluded_at", UTCDateTimeText),
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
    CheckConstraint("priority BETWEEN 0 AND 100", name="priority"),
    CheckConstraint(
        "assessment_status IN ('open','partial','blocked','completed')",
        name="assessment_status",
    ),
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
