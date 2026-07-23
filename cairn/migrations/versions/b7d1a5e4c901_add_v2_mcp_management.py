"""add V2 MCP management resources

Revision ID: b7d1a5e4c901
Revises: 91f6b2c77a4e
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7d1a5e4c901"
down_revision: Union[str, Sequence[str], None] = "91f6b2c77a4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), server_default="stdio", nullable=False),
        sa.Column("command", sa.String(), server_default="", nullable=False),
        sa.Column("args_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("env_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("url", sa.Text(), server_default="", nullable=False),
        sa.Column("source_format", sa.String(), server_default="json", nullable=False),
        sa.Column("raw_config", sa.Text(), server_default="{}", nullable=False),
        sa.Column("disabled", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "mcp_server_tools",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("source", sa.String(), server_default="manual", nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "name"),
    )


def downgrade() -> None:
    op.drop_table("mcp_server_tools")
    op.drop_table("mcp_servers")
