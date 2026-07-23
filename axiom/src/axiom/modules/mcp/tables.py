from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, String, Table, Text, UniqueConstraint

from axiom.shared.database import metadata


mcp_servers = Table(
    "mcp_servers",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False, unique=True),
    Column("transport", String, nullable=False, default="stdio"),
    Column("command", String, nullable=False, default=""),
    Column("args_json", Text, nullable=False, default="[]"),
    Column("env_json", Text, nullable=False, default="{}"),
    Column("url", Text, nullable=False, default=""),
    Column("source_format", String, nullable=False, default="json"),
    Column("raw_config_json", Text, nullable=False, default="{}"),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("discovery_status", String, nullable=False, default="pending"),
    Column("discovery_error", Text, nullable=False, default=""),
    Column("discovered_at", String, nullable=False, default=""),
    Column("server_info_json", Text, nullable=False, default="{}"),
    Column("instructions", Text, nullable=False, default=""),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)


mcp_tools = Table(
    "mcp_tools",
    metadata,
    Column("id", String, primary_key=True),
    Column("server_id", String, ForeignKey(mcp_servers.c.id, ondelete="CASCADE"), nullable=False),
    Column("name", String, nullable=False),
    Column("title", String, nullable=False, default=""),
    Column("description", Text, nullable=False, default=""),
    Column("input_schema_json", Text, nullable=False, default="{}"),
    Column("output_schema_json", Text, nullable=False, default="{}"),
    Column("annotations_json", Text, nullable=False, default="{}"),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("source", String, nullable=False, default="manual"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("server_id", "name"),
)
