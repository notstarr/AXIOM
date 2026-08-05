from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, Integer, String, Table

from axiom.shared.database import JSONText, UTCDateTimeText, metadata


workers = Table(
    "workers",
    metadata,
    Column("name", String, primary_key=True),
    Column("worker_type", String, nullable=False),
    Column("capabilities_json", JSONText, nullable=False),
    Column("max_running", Integer, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("config_json", JSONText, nullable=False),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("cooldown_until", UTCDateTimeText),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    CheckConstraint("max_running > 0", name="max_running"),
)
