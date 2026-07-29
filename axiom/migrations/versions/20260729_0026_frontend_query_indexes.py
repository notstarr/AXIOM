"""Add indexes used by paginated frontend read models."""

from __future__ import annotations

from alembic import op


revision = "20260729_0026"
down_revision = "20260729_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_artifacts_active_created", "artifacts", ["archived", "created_at"])
    op.create_index(
        "ix_artifacts_task_active_created",
        "artifacts",
        ["task_id", "archived", "created_at"],
    )
    op.create_index("ix_artifacts_type_created", "artifacts", ["artifact_type", "created_at"])
    op.create_index(
        "ix_vulnerabilities_active_updated",
        "vulnerabilities",
        ["archived", "updated_at"],
    )
    op.create_index(
        "ix_vulnerabilities_status_updated",
        "vulnerabilities",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_vulnerabilities_severity_updated",
        "vulnerabilities",
        ["severity", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_vulnerabilities_severity_updated", table_name="vulnerabilities")
    op.drop_index("ix_vulnerabilities_status_updated", table_name="vulnerabilities")
    op.drop_index("ix_vulnerabilities_active_updated", table_name="vulnerabilities")
    op.drop_index("ix_artifacts_type_created", table_name="artifacts")
    op.drop_index("ix_artifacts_task_active_created", table_name="artifacts")
    op.drop_index("ix_artifacts_active_created", table_name="artifacts")
