"""Bind Context and Reason Workspace records to one lease attempt."""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0009"
down_revision = "20260723_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("context_revisions") as batch:
        batch.add_column(sa.Column("lease_id", sa.String(), nullable=True))
        batch.create_unique_constraint(
            "uq_context_revisions_task_id_intent_id_lease_id",
            ["task_id", "intent_id", "lease_id"],
        )
        batch.create_index(
            "ix_context_revisions_task_intent",
            ["task_id", "intent_id"],
            unique=False,
        )

    with op.batch_alter_table("reason_workspaces") as batch:
        batch.drop_constraint(
            "uq_reason_workspaces_task_id_intent_id",
            type_="unique",
        )
        batch.add_column(sa.Column("lease_id", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("result_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch.create_unique_constraint(
            "uq_reason_workspaces_task_id_intent_id_lease_id",
            ["task_id", "intent_id", "lease_id"],
        )

    op.execute(
        "UPDATE reason_workspaces "
        "SET lease_id = 'legacy_' || id "
        "WHERE lease_id IS NULL"
    )
    with op.batch_alter_table("reason_workspaces") as batch:
        batch.alter_column("lease_id", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("reason_workspaces") as batch:
        batch.drop_constraint(
            "uq_reason_workspaces_task_id_intent_id_lease_id",
            type_="unique",
        )
        batch.drop_column("result_json")
        batch.drop_column("lease_id")
        batch.create_unique_constraint(
            "uq_reason_workspaces_task_id_intent_id",
            ["task_id", "intent_id"],
        )

    with op.batch_alter_table("context_revisions") as batch:
        batch.drop_index("ix_context_revisions_task_intent")
        batch.drop_constraint(
            "uq_context_revisions_task_id_intent_id_lease_id",
            type_="unique",
        )
        batch.drop_column("lease_id")
