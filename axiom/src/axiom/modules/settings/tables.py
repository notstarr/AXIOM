from __future__ import annotations

from sqlalchemy import Column, Integer, String, Table

from axiom.shared.database import JSONText, UTCDateTimeText, metadata


runtime_settings = Table(
    "runtime_settings",
    metadata,
    Column("id", String, primary_key=True),
    Column("config_json", JSONText, nullable=False),
    Column("version", Integer, nullable=False),
    Column("updated_by", String, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("applied_version", Integer, nullable=False, default=0),
    Column("applied_at", UTCDateTimeText),
)
