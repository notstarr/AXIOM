from __future__ import annotations

from sqlalchemy import Column, Index, String, Table, Text

from axiom.shared.database import JSONText, UTCDateTimeText, metadata


audit_events = Table(
    "audit_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String),
    Column("task_id", String),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("plan_run_id", String),
    Column("review_run_id", String),
    Column("workspace_id", String),
    Column("worker", String, nullable=False, default=""),
    Column("event_type", String, nullable=False),
    Column("level", String, nullable=False),
    Column("message", Text, nullable=False),
    Column("payload_json", JSONText, nullable=False, default="{}"),
    Column("created_at", UTCDateTimeText, nullable=False),
)
Index("ix_audit_events_task_created", audit_events.c.task_id, audit_events.c.created_at)
Index("ix_audit_events_project_created", audit_events.c.project_id, audit_events.c.created_at)
