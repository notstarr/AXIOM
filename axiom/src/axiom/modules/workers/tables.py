from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, Integer, String, Table, Text

from axiom.shared.database import metadata


workers = Table(
    "workers",
    metadata,
    Column("name", String, primary_key=True),
    Column("worker_type", String, nullable=False),
    Column("capabilities_json", Text, nullable=False),
    Column("max_running", Integer, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("config_json", Text, nullable=False),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("cooldown_until", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("max_running > 0", name="max_running"),
)
