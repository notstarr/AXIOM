"""Add MCP server registry and discovered tool metadata."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0012"
down_revision = "20260723_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("command", sa.String(), nullable=False),
        sa.Column("args_json", sa.Text(), nullable=False),
        sa.Column("env_json", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_format", sa.String(), nullable=False),
        sa.Column("raw_config_json", sa.Text(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("discovery_status", sa.String(), nullable=False),
        sa.Column("discovery_error", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.String(), nullable=False),
        sa.Column("server_info_json", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mcp_servers"),
        sa.UniqueConstraint("name", name="uq_mcp_servers_name"),
    )
    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.Text(), nullable=False),
        sa.Column("output_schema_json", sa.Text(), nullable=False),
        sa.Column("annotations_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["mcp_servers.id"],
            name="fk_mcp_tools_server_id_mcp_servers",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mcp_tools"),
        sa.UniqueConstraint("server_id", "name", name="uq_mcp_tools_server_id_name"),
    )


def downgrade() -> None:
    op.drop_table("mcp_tools")
    op.drop_table("mcp_servers")
