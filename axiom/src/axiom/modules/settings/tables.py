from __future__ import annotations

from sqlalchemy import Column, Integer, String, Table, Text

from axiom.shared.database import metadata


runtime_settings = Table(
    "runtime_settings",
    metadata,
    Column("id", String, primary_key=True),
    Column("config_json", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("updated_by", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
