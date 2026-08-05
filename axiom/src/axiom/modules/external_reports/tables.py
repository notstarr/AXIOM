from __future__ import annotations

from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


external_report_attempts = Table(
    "external_report_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, nullable=False),
    Column("vulnerability_id", String, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("payload_json", JSONText, nullable=False),
    Column("response_status", Integer),
    Column("response_body", Text, nullable=False, default=""),
    Column("error", Text, nullable=False, default=""),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("completed_at", UTCDateTimeText),
    ForeignKeyConstraint(
        ["task_id", "vulnerability_id"],
        [vulnerabilities.c.task_id, vulnerabilities.c.id],
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "task_id",
        "vulnerability_id",
        "attempt_no",
        name="uq_external_report_attempts_task_vulnerability_attempt",
    ),
)
Index(
    "ix_external_report_attempts_task_created",
    external_report_attempts.c.task_id,
    external_report_attempts.c.created_at,
)
