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

from axiom.modules.artifacts.tables import artifacts
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
    Column("format_version", Integer, nullable=False, default=1, server_default="1"),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="RESTRICT",
    ),
)
Index("ix_vulnerabilities_task_updated", vulnerabilities.c.task_id, vulnerabilities.c.updated_at)


vulnerability_artifacts = Table(
    "vulnerability_artifacts",
    metadata,
    Column("task_id", String, nullable=False),
    Column("vulnerability_id", String, nullable=False),
    Column("artifact_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("display_order", Integer, nullable=False, default=0, server_default="0"),
    PrimaryKeyConstraint("task_id", "vulnerability_id", "artifact_id"),
    ForeignKeyConstraint(
        ["task_id", "vulnerability_id"],
        [vulnerabilities.c.task_id, vulnerabilities.c.id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["task_id", "artifact_id"],
        [artifacts.c.task_id, artifacts.c.id],
        ondelete="RESTRICT",
    ),
    CheckConstraint("role IN ('finding','evidence')", name="role"),
)
Index(
    "ix_vulnerability_artifacts_task_vulnerability_role",
    vulnerability_artifacts.c.task_id,
    vulnerability_artifacts.c.vulnerability_id,
    vulnerability_artifacts.c.role,
)
