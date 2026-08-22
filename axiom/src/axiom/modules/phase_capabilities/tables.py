from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Index, String, Table, Text, UniqueConstraint

from axiom.shared.database import UTCDateTimeText, metadata


phase_capability_bindings = Table(
    "phase_capability_bindings",
    metadata,
    Column("scope", String, nullable=False),
    Column("capability_kind", String, nullable=False),
    Column("capability_key", Text, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    CheckConstraint(
        "scope IN ('common','bootstrap','reason','explore','plan','review')",
        name="scope",
    ),
    CheckConstraint(
        "capability_kind IN ('skill','mcp_tool','axiom_tool')",
        name="capability_kind",
    ),
    UniqueConstraint(
        "scope",
        "capability_kind",
        "capability_key",
        name="uq_phase_capability_bindings_scope_kind_key",
    ),
)

Index(
    "ix_phase_capability_bindings_kind_key",
    phase_capability_bindings.c.capability_kind,
    phase_capability_bindings.c.capability_key,
)
