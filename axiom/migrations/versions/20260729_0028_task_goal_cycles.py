"""Version Task Goals and support completed Task continuations."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_0028"
down_revision = "20260729_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_goal_cycles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("continuation_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("focus", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.CheckConstraint("status IN ('active','completed')", name="ck_task_goal_cycles_status"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "sequence", name="uq_task_goal_cycles_task_sequence"),
    )
    op.create_index(
        "ix_task_goal_cycles_task_sequence",
        "task_goal_cycles",
        ["task_id", "sequence"],
    )
    op.add_column("tasks", sa.Column("current_goal_cycle_id", sa.String(), nullable=True))
    op.add_column("intents", sa.Column("goal_cycle_id", sa.String(), nullable=True))
    op.add_column("reason_runs", sa.Column("goal_cycle_id", sa.String(), nullable=True))
    op.add_column("goal_evaluations", sa.Column("goal_cycle_id", sa.String(), nullable=True))

    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                "SELECT id, goal, status, created_at, updated_at, completed_at "
                "FROM tasks ORDER BY created_at, id"
            )
        ).mappings()
    )
    for index, row in enumerate(rows, start=1):
        cycle_id = f"gc{index:03d}"
        completed = row["status"] == "completed"
        bind.execute(
            sa.text(
                "INSERT INTO task_goal_cycles "
                "(id, task_id, sequence, goal, status, continuation_reason, focus, actor, "
                "started_at, completed_at) "
                "VALUES (:id, :task_id, 1, :goal, :status, '', '', 'system.migration', "
                ":started_at, :completed_at)"
            ),
            {
                "id": cycle_id,
                "task_id": row["id"],
                "goal": row["goal"],
                "status": "completed" if completed else "active",
                "started_at": row["created_at"],
                "completed_at": (
                    row["completed_at"] or row["updated_at"] if completed else None
                ),
            },
        )
        for table in ("tasks", "intents", "reason_runs", "goal_evaluations"):
            column = "current_goal_cycle_id" if table == "tasks" else "goal_cycle_id"
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :cycle_id WHERE task_id = :task_id")
                if table != "tasks"
                else sa.text("UPDATE tasks SET current_goal_cycle_id = :cycle_id WHERE id = :task_id"),
                {"cycle_id": cycle_id, "task_id": row["id"]},
            )

    bind.execute(
        sa.text(
            "INSERT INTO id_counters(scope, kind, next_value) VALUES ('global', 'goal_cycle', :next) "
            "ON CONFLICT(scope, kind) DO UPDATE SET next_value = "
            "CASE WHEN next_value < :next THEN :next ELSE next_value END"
        ),
        {"next": len(rows) + 1},
    )


def downgrade() -> None:
    op.drop_column("goal_evaluations", "goal_cycle_id")
    op.drop_column("reason_runs", "goal_cycle_id")
    op.drop_column("intents", "goal_cycle_id")
    op.drop_column("tasks", "current_goal_cycle_id")
    op.drop_index("ix_task_goal_cycles_task_sequence", table_name="task_goal_cycles")
    op.drop_table("task_goal_cycles")
    op.execute("DELETE FROM id_counters WHERE scope = 'global' AND kind = 'goal_cycle'")
