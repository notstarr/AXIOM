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
    Column("review_run_id", String),
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
    ForeignKeyConstraint(
        ["task_id", "review_run_id"],
        ["vulnerability_review_runs.task_id", "vulnerability_review_runs.id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("task_id", "storage_key"),
)
Index("ix_artifacts_active_created", artifacts.c.archived, artifacts.c.created_at)
Index(
    "ix_artifacts_task_active_created",
    artifacts.c.task_id,
    artifacts.c.archived,
    artifacts.c.created_at,
)
Index(
    "ix_artifacts_task_active_intent_created",
    artifacts.c.task_id,
    artifacts.c.archived,
    artifacts.c.intent_id,
    artifacts.c.created_at,
)
Index(
    "ix_artifacts_task_active_review_created",
    artifacts.c.task_id,
    artifacts.c.archived,
    artifacts.c.review_run_id,
    artifacts.c.created_at,
)
Index("ix_artifacts_type_created", artifacts.c.artifact_type, artifacts.c.created_at)
