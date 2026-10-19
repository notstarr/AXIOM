"""Add webshell connection and exec-log tables (SQLite branch)."""

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261016_0054"
down_revision = "20261016_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    schema = AXIOM_SCHEMA
    if not inspector.has_table("webshell_connections", schema=schema):
        op.create_table(
            "webshell_connections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("family", sa.String(), nullable=False, server_default="posix"),
            sa.Column("request_json", sa.Text(), nullable=False),
            sa.Column("response_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="untested"),
            sa.Column("last_check_at", sa.Text()),
            sa.Column("last_check_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("remark", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("source_intent_id", sa.String()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.PrimaryKeyConstraint("task_id", "id"),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["task_id", "source_intent_id"],
                ["intents.task_id", "intents.id"],
                ondelete="SET NULL",
            ),
            schema=schema,
        )
        op.create_index(
            "ix_webshell_connections_task_status",
            "webshell_connections",
            ["task_id", "status"],
            schema=schema,
        )
    if not inspector.has_table("webshell_exec_logs", schema=schema):
        op.create_table(
            "webshell_exec_logs",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("connection_id", sa.String(), nullable=False),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("output", sa.Text(), nullable=False, server_default=""),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("intent_id", sa.String()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("task_id", "id"),
            sa.ForeignKeyConstraint(
                ["task_id", "connection_id"],
                ["webshell_connections.task_id", "webshell_connections.id"],
                ondelete="CASCADE",
            ),
            schema=schema,
        )
        op.create_index(
            "ix_webshell_exec_logs_connection",
            "webshell_exec_logs",
            ["task_id", "connection_id"],
            schema=schema,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("webshell_exec_logs", schema=AXIOM_SCHEMA):
        op.drop_table("webshell_exec_logs", schema=AXIOM_SCHEMA)
    if inspector.has_table("webshell_connections", schema=AXIOM_SCHEMA):
        op.drop_table("webshell_connections", schema=AXIOM_SCHEMA)
