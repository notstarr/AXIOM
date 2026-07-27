from __future__ import annotations

from sqlalchemy import Boolean, Column, ForeignKey, Index, String, Table, Text

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


goal_evaluations = Table(
    "goal_evaluations",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey(tasks.c.id, ondelete="CASCADE"), nullable=False),
    Column("intent_id", String),
    Column("reason_run_id", String),
    Column("workspace_id", String),
    Column("achieved", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("evidence_refs_json", Text, nullable=False),
    Column("evaluator", String, nullable=False),
    Column("created_at", String, nullable=False),
)
Index("ix_goal_evaluations_task_created", goal_evaluations.c.task_id, goal_evaluations.c.created_at)
