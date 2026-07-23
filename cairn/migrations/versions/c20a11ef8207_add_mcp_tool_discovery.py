"""add MCP tool discovery metadata

Revision ID: c20a11ef8207
Revises: b7d1a5e4c901
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c20a11ef8207"
down_revision: Union[str, Sequence[str], None] = "b7d1a5e4c901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("discovery_status", sa.String(), server_default="pending", nullable=False))
    op.add_column("mcp_servers", sa.Column("discovery_error", sa.Text(), server_default="", nullable=False))
    op.add_column("mcp_servers", sa.Column("discovered_at", sa.String(), server_default="", nullable=False))
    op.add_column("mcp_servers", sa.Column("server_info_json", sa.Text(), server_default="{}", nullable=False))
    op.add_column("mcp_servers", sa.Column("instructions", sa.Text(), server_default="", nullable=False))
    op.add_column("mcp_server_tools", sa.Column("title", sa.String(), server_default="", nullable=False))
    op.add_column("mcp_server_tools", sa.Column("input_schema_json", sa.Text(), server_default="{}", nullable=False))
    op.add_column("mcp_server_tools", sa.Column("output_schema_json", sa.Text(), server_default="{}", nullable=False))
    op.add_column("mcp_server_tools", sa.Column("annotations_json", sa.Text(), server_default="{}", nullable=False))


def downgrade() -> None:
    op.drop_column("mcp_server_tools", "annotations_json")
    op.drop_column("mcp_server_tools", "output_schema_json")
    op.drop_column("mcp_server_tools", "input_schema_json")
    op.drop_column("mcp_server_tools", "title")
    op.drop_column("mcp_servers", "instructions")
    op.drop_column("mcp_servers", "server_info_json")
    op.drop_column("mcp_servers", "discovered_at")
    op.drop_column("mcp_servers", "discovery_error")
    op.drop_column("mcp_servers", "discovery_status")
