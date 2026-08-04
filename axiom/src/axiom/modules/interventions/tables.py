from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, Index, PrimaryKeyConstraint, String, Table, Text

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import metadata


intent_interventions = Table(
    "intent_interventions",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("intent_id", String, nullable=False),
    Column("lease_id", String, nullable=False),
    Column("actor", String, nullable=False, default="human.frontend"),
    Column("message", Text, nullable=False),
    Column("client_message_id", String, nullable=False),
    Column("status", String, nullable=False, default="pending"),
    Column("error", Text, nullable=False, default=""),
    Column("created_at", String, nullable=False),
    Column("sent_at", String),
    Column("updated_at", String, nullable=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "status IN ('pending','sent','accepted','failed','expired')",
        name="status",
    ),
)
Index("uq_intent_interventions_client", intent_interventions.c.task_id, intent_interventions.c.intent_id, intent_interventions.c.client_message_id, unique=True)
Index("ix_intent_interventions_queue", intent_interventions.c.task_id, intent_interventions.c.intent_id, intent_interventions.c.lease_id, intent_interventions.c.status, intent_interventions.c.created_at)
