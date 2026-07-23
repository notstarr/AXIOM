"""Create Project, Task and Graph foundations.

Revision ID: 20260723_0001
Revises:
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "id_counters",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("next_value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "kind", name="pk_id_counters"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_projects_status"),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("origin_host", sa.String(), nullable=False),
        sa.Column("origin_scheme", sa.String(), nullable=True),
        sa.Column("origin_port", sa.String(), nullable=True),
        sa.Column("origin_input", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("worker_pool_json", sa.Text(), nullable=False),
        sa.Column("guard_enabled", sa.Boolean(), nullable=False),
        sa.Column("external_report_enabled", sa.Boolean(), nullable=False),
        sa.Column("legacy_readonly", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','stopped','completed','failed')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "phase IN ('bootstrap','reason','explore','waiting','none')",
            name="ck_tasks_phase",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_tasks_project_id_projects", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
    )
    op.create_table(
        "task_hints",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("creator", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_task_hints_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id", "id", name="pk_task_hints"),
    )
    op.create_table(
        "facts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_facts_task_id_tasks", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id", "id", name="pk_facts"),
    )
    op.create_table(
        "intents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("todo_id", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("creator", sa.String(), nullable=False),
        sa.Column("worker", sa.String(), nullable=True),
        sa.Column("worker_pool_json", sa.Text(), nullable=False),
        sa.Column("targets_json", sa.Text(), nullable=False),
        sa.Column("execute_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("conclude_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("last_heartbeat_at", sa.String(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("infrastructure_retry_count", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.String(), nullable=True),
        sa.Column("last_failure_kind", sa.String(), nullable=False),
        sa.Column("failure_class", sa.String(), nullable=False),
        sa.Column("to_fact_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("concluded_at", sa.String(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','claimed','running','concluding','completed','failed','cancelled')",
            name="ck_intents_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_intents_task_id_tasks", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "to_fact_id"],
            ["facts.task_id", "facts.id"],
            name="fk_intents_task_id_to_fact_id_facts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", "id", name="pk_intents"),
    )
    op.create_table(
        "intent_sources",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("intent_id", sa.String(), nullable=False),
        sa.Column("fact_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id", "fact_id"],
            ["facts.task_id", "facts.id"],
            name="fk_intent_sources_task_id_fact_id_facts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "intent_id"],
            ["intents.task_id", "intents.id"],
            name="fk_intent_sources_task_id_intent_id_intents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "intent_id", "fact_id", name="pk_intent_sources"),
    )


def downgrade() -> None:
    op.drop_table("intent_sources")
    op.drop_table("intents")
    op.drop_table("facts")
    op.drop_table("task_hints")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("id_counters")
