"""Add version-controlled AXIOM Tool catalog and Worker phase bindings."""

from alembic import op
import sqlalchemy as sa


revision = "20260803_0030"
down_revision = "20260803_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "axiom_tools",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("package_path", sa.Text(), nullable=False),
        sa.Column("entrypoint", sa.Text(), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.Text(), nullable=False),
        sa.Column("supported_phases_json", sa.Text(), nullable=False),
        sa.Column("risk_tags_json", sa.Text(), nullable=False),
        sa.Column("package_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("discovered_at", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "source IN ('bundled','managed')",
            name="ck_axiom_tools_source",
        ),
        sa.CheckConstraint(
            "status IN ('ready','invalid','missing')",
            name="ck_axiom_tools_status",
        ),
        sa.PrimaryKeyConstraint("key", name="pk_axiom_tools"),
    )
    op.create_table(
        "worker_axiom_tool_bindings",
        sa.Column("worker_name", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("tool_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "phase IN ('bootstrap','reason','explore')",
            name="ck_worker_axiom_tool_bindings_phase",
        ),
        sa.ForeignKeyConstraint(
            ["tool_key"],
            ["axiom_tools.key"],
            ondelete="CASCADE",
            name="fk_worker_axiom_tool_bindings_tool_key_axiom_tools",
        ),
        sa.ForeignKeyConstraint(
            ["worker_name"],
            ["workers.name"],
            ondelete="CASCADE",
            name="fk_worker_axiom_tool_bindings_worker_name_workers",
        ),
        sa.PrimaryKeyConstraint(
            "worker_name",
            "phase",
            "tool_key",
            name="pk_worker_axiom_tool_bindings",
        ),
    )
    op.create_index(
        "ix_worker_axiom_tool_bindings_tool_phase",
        "worker_axiom_tool_bindings",
        ["tool_key", "phase"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_worker_axiom_tool_bindings_tool_phase",
        table_name="worker_axiom_tool_bindings",
    )
    op.drop_table("worker_axiom_tool_bindings")
    op.drop_table("axiom_tools")
