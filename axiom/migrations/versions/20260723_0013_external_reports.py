"""Persist external vulnerability report attempts."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0013"
down_revision = "20260723_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_report_attempts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("vulnerability_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id", "vulnerability_id"],
            ["vulnerabilities.task_id", "vulnerabilities.id"],
            name="fk_external_report_attempts_task_vulnerability",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_report_attempts"),
        sa.UniqueConstraint(
            "task_id",
            "vulnerability_id",
            "attempt_no",
            name="uq_external_report_attempts_task_vulnerability_attempt",
        ),
    )
    op.create_index(
        "ix_external_report_attempts_task_created",
        "external_report_attempts",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_report_attempts_task_created",
        table_name="external_report_attempts",
    )
    op.drop_table("external_report_attempts")
