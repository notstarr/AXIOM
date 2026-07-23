from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.projects.tables import projects
from axiom.shared.database import metadata


tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, ForeignKey(projects.c.id, ondelete="RESTRICT"), nullable=False),
    Column("title", Text, nullable=False),
    Column("origin_host", String, nullable=False),
    Column("origin_scheme", String),
    Column("origin_port", String),
    Column("origin_input", Text, nullable=False),
    Column("goal", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("phase", String, nullable=False),
    Column("scope_json", Text, nullable=False),
    Column("worker_pool_json", Text, nullable=False),
    Column("guard_enabled", Boolean, nullable=False, default=False),
    Column("external_report_enabled", Boolean, nullable=False, default=False),
    Column("legacy_readonly", Boolean, nullable=False, default=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("completed_at", String),
    CheckConstraint("status IN ('running','stopped','completed','failed')", name="status"),
    CheckConstraint("phase IN ('bootstrap','reason','explore','waiting','none')", name="phase"),
)


task_hints = Table(
    "task_hints",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("content", Text, nullable=False),
    Column("creator", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
)
