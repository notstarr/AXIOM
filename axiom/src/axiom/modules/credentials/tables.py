from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from axiom.modules.projects.tables import projects
from axiom.shared.database import UTCDateTimeText, metadata


credentials = Table(
    "credentials",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "project_id",
        String,
        ForeignKey(projects.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("notes", Text, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
)
Index("ix_credentials_project_enabled", credentials.c.project_id, credentials.c.enabled)


credential_refreshes = Table(
    "credential_refreshes",
    metadata,
    Column("id", String, primary_key=True),
    Column(
        "credential_id",
        String,
        ForeignKey(credentials.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("content", Text, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    UniqueConstraint("credential_id", name="uq_credential_refreshes_credential_id"),
)
