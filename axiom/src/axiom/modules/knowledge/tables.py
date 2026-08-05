from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, Index, PrimaryKeyConstraint, String, Table, Text

from axiom.modules.projects.tables import projects
from axiom.shared.database import UTCDateTimeText, metadata


project_knowledge = Table(
    "project_knowledge",
    metadata,
    Column("id", String, nullable=False),
    Column("project_id", String, ForeignKey(projects.c.id, ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("category", String, nullable=False, default="note"),
    Column("created_by", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("project_id", "id"),
)
Index("ix_project_knowledge_project_updated", project_knowledge.c.project_id, project_knowledge.c.updated_at)
