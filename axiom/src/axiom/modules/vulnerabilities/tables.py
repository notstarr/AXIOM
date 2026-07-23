from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


vulnerabilities = Table(
    "vulnerabilities",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("intent_id", String),
    Column("title", Text, nullable=False),
    Column("type", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("description", Text, nullable=False),
    Column("prerequisites", Text, nullable=False, default=""),
    Column("impact", Text, nullable=False),
    Column("reproduction_steps_json", Text, nullable=False, default="[]"),
    Column("remediation", Text, nullable=False, default=""),
    Column("curl_command", Text, nullable=False, default=""),
    Column("evidence_json", Text, nullable=False, default="[]"),
    Column("affected_target", String, nullable=False, default=""),
    Column("status", String, nullable=False, default="待验证"),
    Column("status_reason", Text, nullable=False, default=""),
    Column("source", String, nullable=False),
    Column("reported_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="RESTRICT",
    ),
)
Index("ix_vulnerabilities_task_updated", vulnerabilities.c.task_id, vulnerabilities.c.updated_at)
