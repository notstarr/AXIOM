from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


task_goal_cycles = Table(
    "task_goal_cycles",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("goal", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("continuation_reason", Text, nullable=False, default=""),
    Column("focus", Text, nullable=False, default=""),
    Column("actor", String, nullable=False),
    Column("started_at", UTCDateTimeText, nullable=False),
    Column("completed_at", UTCDateTimeText),
    CheckConstraint("status IN ('active','completed')", name="status"),
    UniqueConstraint("task_id", "sequence", name="uq_task_goal_cycles_task_sequence"),
)
Index("ix_task_goal_cycles_task_sequence", task_goal_cycles.c.task_id, task_goal_cycles.c.sequence)


goal_evaluations = Table(
    "goal_evaluations",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("goal_cycle_id", String),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("workspace_id", String),
    Column("achieved", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("evidence_refs_json", JSONText, nullable=False),
    Column("evaluator", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
)
Index("ix_goal_evaluations_task_created", goal_evaluations.c.task_id, goal_evaluations.c.created_at)
