from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import UTCDateTimeText, metadata


context_revisions = Table(
    "context_revisions",
    metadata,
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("phase", String, nullable=False),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("review_run_id", String),
    Column("lease_id", String),
    Column("manifest_key", Text, nullable=False),
    Column("content_hash", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    PrimaryKeyConstraint("task_id", "revision"),
    UniqueConstraint("task_id", "intent_id", "lease_id"),
    UniqueConstraint("task_id", "reason_run_id", "lease_id"),
    UniqueConstraint("task_id", "review_run_id", "lease_id"),
)
Index("ix_context_revisions_task_intent", context_revisions.c.task_id, context_revisions.c.intent_id)
Index(
    "ix_context_revisions_task_reason_run",
    context_revisions.c.task_id,
    context_revisions.c.reason_run_id,
)
Index(
    "ix_context_revisions_task_review_run",
    context_revisions.c.task_id,
    context_revisions.c.review_run_id,
)
