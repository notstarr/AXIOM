"""Add C2 listener/session/task tables (PostgreSQL branch)."""

from alembic import op
import sqlalchemy as sa

from axiom.shared.database import AXIOM_SCHEMA


revision = "20261016_pg0025"
down_revision = "20261016_pg0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    schema = AXIOM_SCHEMA
    if not inspector.has_table("c2_listeners", schema=schema):
        op.create_table(
            "c2_listeners",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False, server_default="http"),
            sa.Column("host", sa.String(), nullable=False, server_default="0.0.0.0"),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="stopped"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.PrimaryKeyConstraint("task_id", "id"),
            sa.ForeignKeyConstraint(["task_id"], ["axiom.tasks.id"], ondelete="CASCADE"),
            schema=schema,
        )
        op.create_index("ix_c2_listeners_task_status", "c2_listeners", ["task_id", "status"], schema=schema)
    if not inspector.has_table("c2_sessions", schema=schema):
        op.create_table(
            "c2_sessions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("listener_id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False, server_default="http"),
            sa.Column("remote", sa.String(), nullable=False, server_default=""),
            sa.Column("info_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="active"),
            sa.Column("sleep_seconds", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("last_seen_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("task_id", "id"),
            sa.ForeignKeyConstraint(["task_id"], ["axiom.tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["task_id", "listener_id"],
                ["axiom.c2_listeners.task_id", "axiom.c2_listeners.id"],
                ondelete="CASCADE",
            ),
            schema=schema,
        )
        op.create_index("ix_c2_sessions_task_listener", "c2_sessions", ["task_id", "listener_id"], schema=schema)
    if not inspector.has_table("c2_tasks", schema=schema):
        op.create_table(
            "c2_tasks",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("command", sa.Text(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("output", sa.Text(), nullable=False, server_default=""),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("intent_id", sa.String()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("task_id", "id"),
            sa.ForeignKeyConstraint(["task_id"], ["axiom.tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["task_id", "session_id"],
                ["axiom.c2_sessions.task_id", "axiom.c2_sessions.id"],
                ondelete="CASCADE",
            ),
            schema=schema,
        )
        op.create_index(
            "ix_c2_tasks_session_status",
            "c2_tasks",
            ["task_id", "session_id", "status"],
            schema=schema,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("c2_tasks", "c2_sessions", "c2_listeners"):
        if inspector.has_table(table, schema=AXIOM_SCHEMA):
            op.drop_table(table, schema=AXIOM_SCHEMA)
