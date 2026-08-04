from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.workers.tables import workers
from axiom.shared.database import metadata


axiom_tools = Table(
    "axiom_tools",
    metadata,
    Column("key", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("version", String, nullable=False),
    Column("description", Text, nullable=False),
    Column("source", String, nullable=False),
    Column("package_path", Text, nullable=False),
    Column("entrypoint", Text, nullable=False),
    Column("manifest_json", Text, nullable=False),
    Column("input_schema_json", Text, nullable=False),
    Column("supported_phases_json", Text, nullable=False),
    Column("risk_tags_json", Text, nullable=False),
    Column("package_hash", String, nullable=False),
    Column("status", String, nullable=False),
    Column("status_error", Text, nullable=False, default=""),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("discovered_at", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("source IN ('bundled','managed')", name="source"),
    CheckConstraint("status IN ('ready','invalid','missing')", name="status"),
)


worker_axiom_tool_bindings = Table(
    "worker_axiom_tool_bindings",
    metadata,
    Column(
        "worker_name",
        String,
        ForeignKey(workers.c.name, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("phase", String, nullable=False),
    Column(
        "tool_key",
        String,
        ForeignKey(axiom_tools.c.key, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    PrimaryKeyConstraint("worker_name", "phase", "tool_key"),
    CheckConstraint("phase IN ('bootstrap','reason','explore','review')", name="phase"),
)

Index(
    "ix_worker_axiom_tool_bindings_tool_phase",
    worker_axiom_tool_bindings.c.tool_key,
    worker_axiom_tool_bindings.c.phase,
)
