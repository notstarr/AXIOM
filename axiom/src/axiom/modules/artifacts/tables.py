from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("intent_id", String),
    Column("artifact_type", String, nullable=False),
    Column("storage_key", Text, nullable=False),
    Column("summary", Text, nullable=False, default=""),
    Column("content_hash", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("created_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("task_id", "storage_key"),
)
