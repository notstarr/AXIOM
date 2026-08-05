from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, String, Table, Text

from axiom.shared.database import UTCDateTimeText, metadata


projects = Table(
    "projects",
    metadata,
    Column("id", String, primary_key=True),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("status", String, nullable=False, default="active"),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    CheckConstraint("status IN ('active','archived')", name="status"),
)
