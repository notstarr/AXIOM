from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, Index, String, Table, Text

from axiom.modules.projects.tables import projects
from axiom.shared.database import metadata


credentials = Table(
    "credentials",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, ForeignKey(projects.c.id, ondelete="CASCADE"), nullable=False),
    Column("name", String, nullable=False),
    Column("credential_type", String, nullable=False),
    Column("target_host", String, nullable=False, default=""),
    Column("data_json", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
Index("ix_credentials_project_enabled", credentials.c.project_id, credentials.c.enabled)
