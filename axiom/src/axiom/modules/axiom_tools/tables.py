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
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


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
    Column("manifest_json", JSONText, nullable=False),
    Column("input_schema_json", JSONText, nullable=False),
    Column("supported_phases_json", JSONText, nullable=False),
    Column("risk_tags_json", JSONText, nullable=False),
    Column("package_hash", String, nullable=False),
    Column("status", String, nullable=False),
    Column("status_error", Text, nullable=False, default=""),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("discovered_at", UTCDateTimeText, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
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
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    PrimaryKeyConstraint("worker_name", "phase", "tool_key"),
    CheckConstraint("phase IN ('bootstrap','reason','explore','review')", name="phase"),
)

Index(
    "ix_worker_axiom_tool_bindings_tool_phase",
    worker_axiom_tool_bindings.c.tool_key,
    worker_axiom_tool_bindings.c.phase,
)
