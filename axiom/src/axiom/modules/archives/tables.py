from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import UTCDateTimeText, metadata


archive_batches = Table(
    "archive_batches",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("root_type", String, nullable=False),
    Column("root_id", String, nullable=False),
    Column("status", String, nullable=False, default="active"),
    Column("reason", Text, nullable=False, default=""),
    Column("actor", String, nullable=False),
    Column("closure_hash", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("restored_at", UTCDateTimeText),
    Column("restored_by", String),
    CheckConstraint(
        "root_type IN ('fact','intent','artifact','vulnerability','todo')",
        name="root_type",
    ),
    CheckConstraint("status IN ('active','restored')", name="status"),
)
Index("ix_archive_batches_task_status", archive_batches.c.task_id, archive_batches.c.status)
Index(
    "ix_archive_batches_task_root",
    archive_batches.c.task_id,
    archive_batches.c.root_type,
    archive_batches.c.root_id,
)


archive_batch_members = Table(
    "archive_batch_members",
    metadata,
    Column("task_id", String, nullable=False),
    Column(
        "archive_id",
        String,
        ForeignKey(archive_batches.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("entity_type", String, nullable=False),
    Column("entity_id", String, nullable=False),
    Column("relation", String, nullable=False),
    PrimaryKeyConstraint("archive_id", "entity_type", "entity_id"),
    CheckConstraint(
        "entity_type IN ('fact','intent','artifact','vulnerability','todo')",
        name="entity_type",
    ),
    CheckConstraint(
        "relation IN ('root','producer','output','descendant','related')",
        name="relation",
    ),
)
Index(
    "ix_archive_batch_members_task_entity",
    archive_batch_members.c.task_id,
    archive_batch_members.c.entity_type,
    archive_batch_members.c.entity_id,
)
