from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


task_sandboxes = Table(
    "task_sandboxes",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("sandbox_id", String, nullable=False),
    Column("backend", String, nullable=False, server_default="local"),
    Column("status", String, nullable=False, server_default="ready"),
    Column("template_version", String, nullable=False, server_default="default"),
    Column("resource_profile_json", JSONText, nullable=False, server_default="{}"),
    Column("network_policy_json", JSONText, nullable=False, server_default="{}"),
    Column("active_worker_count", Integer, nullable=False, server_default="0"),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("last_used_at", UTCDateTimeText, nullable=False),
    Column("terminated_at", UTCDateTimeText),
    Column("last_error", Text, nullable=False, server_default=""),
    UniqueConstraint("task_id", name="uq_task_sandboxes_task_id"),
    CheckConstraint("status IN ('creating','ready','degraded','terminating','terminated','failed')", name="status"),
)

Index("ix_task_sandboxes_status", task_sandboxes.c.status)
