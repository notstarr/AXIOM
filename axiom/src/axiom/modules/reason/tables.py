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

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


reason_workspaces = Table(
    "reason_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("intent_id", String, nullable=False),
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
    CheckConstraint("status IN ('open','committed','abandoned')", name="status"),
    UniqueConstraint("task_id", "intent_id", "lease_id"),
)
Index("ix_reason_workspaces_task_status", reason_workspaces.c.task_id, reason_workspaces.c.status)
