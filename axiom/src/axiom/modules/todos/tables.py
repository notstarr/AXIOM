from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from axiom.shared.database import metadata


todos = Table(
    "todos",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("category", String, nullable=False),
    Column("theme", String, nullable=False, default=""),
    Column("priority", Integer, nullable=False, default=50),
    Column("status", String, nullable=False, default="pending"),
    Column("created_by", String, nullable=False),
    Column("status_reason", Text, nullable=False, default=""),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("completed_at", String),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    CheckConstraint("priority BETWEEN 0 AND 100", name="priority"),
    CheckConstraint(
        "status IN ('pending','active','completed','rejected','obsolete')",
        name="status",
    ),
    CheckConstraint(
        "category IN ('recon','authentication','authorization','injection',"
        "'business_logic','file','network','configuration','client_side','other')",
        name="category",
    ),
)
Index("ix_todos_task_status_priority", todos.c.task_id, todos.c.status, todos.c.priority)


todo_sources = Table(
    "todo_sources",
    metadata,
    Column("task_id", String, nullable=False),
    Column("todo_id", String, nullable=False),
    Column("source_type", String, nullable=False),
    Column("source_id", String, nullable=False),
    PrimaryKeyConstraint("task_id", "todo_id", "source_type", "source_id"),
    ForeignKeyConstraint(
        ["task_id", "todo_id"],
        [todos.c.task_id, todos.c.id],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "source_type IN ('fact','hint','artifact','vulnerability','knowledge','origin')",
        name="source_type",
    ),
)
