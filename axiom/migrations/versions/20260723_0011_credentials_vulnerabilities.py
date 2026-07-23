"""Add project credentials and task vulnerability records."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0011"
down_revision = "20260723_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("credential_type", sa.String(), nullable=False),
        sa.Column("target_host", sa.String(), nullable=False),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_credentials_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_credentials"),
    )
    op.create_index(
        "ix_credentials_project_enabled",
        "credentials",
        ["project_id", "enabled"],
        unique=False,
    )
    op.create_table(
        "vulnerabilities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("prerequisites", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=False),
        sa.Column("reproduction_steps_json", sa.Text(), nullable=False),
        sa.Column("remediation", sa.Text(), nullable=False),
        sa.Column("curl_command", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("affected_target", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("reported_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_vulnerabilities_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "intent_id"],
            ["intents.task_id", "intents.id"],
            name="fk_vulnerabilities_task_id_intent_id_intents",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", "id", name="pk_vulnerabilities"),
    )
    op.create_index(
        "ix_vulnerabilities_task_updated",
        "vulnerabilities",
        ["task_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_vulnerabilities_task_updated", table_name="vulnerabilities")
    op.drop_table("vulnerabilities")
    op.drop_index("ix_credentials_project_enabled", table_name="credentials")
    op.drop_table("credentials")
