from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


red_team_chains = Table(
    "red_team_chains",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, nullable=False),
    Column("goal_cycle_id", String),
    Column("title", Text, nullable=False),
    Column("initial_principal_json", JSONText, nullable=False),
    Column("target_capabilities_json", JSONText, nullable=False, default="[]"),
    Column("target_assets_json", JSONText, nullable=False, default="[]"),
    Column("status", String, nullable=False, default="open"),
    Column("status_reason", Text, nullable=False, default=""),
    Column("current_state_json", JSONText, nullable=False, default="{}"),
    Column("source_fact_ids_json", JSONText, nullable=False, default="[]"),
    Column("source_intent_ids_json", JSONText, nullable=False, default="[]"),
    Column("created_by", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    UniqueConstraint("task_id", "id", name="uq_red_team_chains_task_id"),
    CheckConstraint(
        "status IN ('open','partial','blocked','completed','abandoned')",
        name="red_team_chain_status",
    ),
)
Index("ix_red_team_chains_task_status", red_team_chains.c.task_id, red_team_chains.c.status)


red_team_results = Table(
    "red_team_results",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, nullable=False),
    Column("chain_id", String, nullable=False),
    Column("intent_id", String, nullable=False),
    Column("result_type", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("principal", String, nullable=False),
    Column("tenant", String, nullable=False, default=""),
    Column("capabilities_before_json", JSONText, nullable=False, default="[]"),
    Column("capabilities_gained_json", JSONText, nullable=False, default="[]"),
    Column("capabilities_after_json", JSONText, nullable=False, default="[]"),
    Column("target_asset", String, nullable=False, default=""),
    Column("data_target_id", String),
    Column("sample_count", Integer, nullable=True),
    Column("sample_bytes", Integer, nullable=True),
    Column("data_fields_json", JSONText, nullable=False, default="[]"),
    Column("data_summary", Text, nullable=False, default=""),
    Column("credential_ref", String),
    Column("evidence_refs_json", JSONText, nullable=False, default="[]"),
    Column("status", String, nullable=False, default="reported"),
    Column("reported_by", String, nullable=False),
    Column("points_declared", Integer, nullable=False, default=0),
    Column("points_awarded", Integer, nullable=False, default=0),
    Column("reviewed_by", String),
    Column("review_reason", Text, nullable=False, default=""),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(["task_id", "chain_id"], [red_team_chains.c.task_id, red_team_chains.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(["task_id", "intent_id"], [intents.c.task_id, intents.c.id], ondelete="RESTRICT"),
    # 复合外键 (task_id, red_team_result_id) -> (task_id, id) 的目标唯一约束
    UniqueConstraint("task_id", "id", name="uq_red_team_results_task_id"),
    CheckConstraint("result_type IN ('permission','data','credential','pivot')", name="red_team_result_type"),
    CheckConstraint(
        "status IN ('reported','reviewing','verified','rejected','duplicate','needs-human')",
        name="red_team_result_status",
    ),
    CheckConstraint("sample_count IS NULL OR sample_count >= 0", name="red_team_sample_count"),
    CheckConstraint("sample_bytes IS NULL OR sample_bytes >= 0", name="red_team_sample_bytes"),
)
Index("ix_red_team_results_task_status", red_team_results.c.task_id, red_team_results.c.status)
Index("ix_red_team_results_chain_created", red_team_results.c.chain_id, red_team_results.c.created_at)
