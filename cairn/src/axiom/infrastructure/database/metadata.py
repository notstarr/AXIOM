from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


id_counters = Table(
    "id_counters",
    metadata,
    Column("scope", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("next_value", Integer, nullable=False, server_default=text("1")),
    PrimaryKeyConstraint("scope", "kind"),
    CheckConstraint("next_value > 0", name="next_value"),
)


projects = Table(
    "projects",
    metadata,
    Column("id", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("status", String, nullable=False, server_default="active"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("status IN ('active', 'archived')", name="status"),
)


tasks = Table(
    "tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("title", String, nullable=False),
    Column("origin_host", String, nullable=False),
    Column("origin_scheme", String),
    Column("origin_port", Integer),
    Column("origin_input", Text, nullable=False),
    Column("goal", Text, nullable=False),
    Column("status", String, nullable=False, server_default="draft"),
    Column("phase", String, nullable=False, server_default="none"),
    Column("bootstrap_enabled", Boolean, nullable=False, server_default=text("1")),
    Column("guard_enabled", Boolean, nullable=False, server_default=text("0")),
    Column("external_report_enabled", Boolean, nullable=False, server_default=text("0")),
    Column("scope_enabled", Boolean, nullable=False, server_default=text("1")),
    Column("scope_json", Text, nullable=False, server_default='{"targets":[],"exclude":[],"notes":""}'),
    Column("worker_pool_json", Text, nullable=False, server_default="[]"),
    Column("legacy_readonly", Boolean, nullable=False, server_default=text("0")),
    Column("reason_worker", String),
    Column("reason_heartbeat_at", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("completed_at", String),
    CheckConstraint("status IN ('draft','running','stopped','completed','failed')", name="status"),
    CheckConstraint("phase IN ('bootstrap','reason','explore','waiting','none')", name="phase"),
)
Index("ix_tasks_project_status", tasks.c.project_id, tasks.c.status)


facts = Table(
    "facts",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("description", Text, nullable=False),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    Column("created_at", String, nullable=False),
    PrimaryKeyConstraint("task_id", "id"),
)


todos = Table(
    "todos",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False),
    Column("category", String, nullable=False),
    Column("theme", String, nullable=False, server_default=""),
    Column("priority", Integer, nullable=False, server_default=text("50")),
    Column("status", String, nullable=False, server_default="pending"),
    Column("created_by", String, nullable=False),
    Column("status_reason", Text, nullable=False, server_default=""),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("completed_at", String),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("task_id", "id"),
    CheckConstraint("priority BETWEEN 0 AND 100", name="priority"),
    CheckConstraint("status IN ('pending','active','completed','rejected','obsolete')", name="status"),
    CheckConstraint(
        "category IN ('recon','authentication','authorization','injection','business_logic','file','network','configuration','client_side','other')",
        name="category",
    ),
)
Index("ix_todos_task_status_priority", todos.c.task_id, todos.c.status, todos.c.priority)


intents = Table(
    "intents",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("todo_id", String),
    Column("to_fact_id", String),
    Column("description", Text, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("creator", String, nullable=False),
    Column("worker", String),
    Column("worker_pool_json", Text, nullable=False, server_default="[]"),
    Column("targets_json", Text, nullable=False, server_default="[]"),
    Column("execute_timeout_seconds", Integer),
    Column("conclude_timeout_seconds", Integer),
    Column("last_heartbeat_at", String),
    Column("retry_count", Integer, nullable=False, server_default=text("0")),
    Column("next_retry_at", String),
    Column("last_failure_kind", String, nullable=False, server_default=""),
    Column("created_at", String, nullable=False),
    Column("concluded_at", String),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(
        ["task_id", "todo_id"],
        ["todos.task_id", "todos.id"],
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["task_id", "to_fact_id"],
        ["facts.task_id", "facts.id"],
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "status IN ('pending','claimed','running','concluding','completed','failed','cancelled')",
        name="status",
    ),
)
Index("ix_intents_task_status_retry", intents.c.task_id, intents.c.status, intents.c.next_retry_at)


intent_sources = Table(
    "intent_sources",
    metadata,
    Column("task_id", String, nullable=False),
    Column("intent_id", String, nullable=False),
    Column("fact_id", String, nullable=False),
    PrimaryKeyConstraint("task_id", "intent_id", "fact_id"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        ["intents.task_id", "intents.id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["task_id", "fact_id"],
        ["facts.task_id", "facts.id"],
        ondelete="RESTRICT",
    ),
)


task_hints = Table(
    "task_hints",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("content", Text, nullable=False),
    Column("creator", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("task_id", "id"),
)


project_knowledge = Table(
    "project_knowledge",
    metadata,
    Column("id", String, nullable=False),
    Column("project_id", String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("content", Text, nullable=False),
    Column("hint", Text, nullable=False, server_default=""),
    Column("created_by", String, nullable=False),
    Column("updated_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("project_id", "id"),
)


todo_sources = Table(
    "todo_sources",
    metadata,
    Column("task_id", String, nullable=False),
    Column("todo_id", String, nullable=False),
    Column("source_type", String, nullable=False),
    Column("source_id", String, nullable=False),
    PrimaryKeyConstraint("task_id", "todo_id", "source_type", "source_id"),
    ForeignKeyConstraint(
        ["task_id", "todo_id"],
        ["todos.task_id", "todos.id"],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "source_type IN ('fact','hint','knowledge','artifact','vulnerability','origin')",
        name="source_type",
    ),
)


reason_workspaces = Table(
    "reason_workspaces",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("status", String, nullable=False, server_default="open"),
    Column("context_revision", Integer, nullable=False),
    Column("draft_json", Text, nullable=False, server_default="{}"),
    Column("worker", String, nullable=False),
    Column("failure_reason", Text, nullable=False, server_default=""),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("committed_at", String),
    CheckConstraint("status IN ('open','committed','abandoned')", name="status"),
)
Index("ix_reason_workspaces_task_status", reason_workspaces.c.task_id, reason_workspaces.c.status)


artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("intent_id", String),
    Column("artifact_type", String, nullable=False),
    Column("path", Text, nullable=False),
    Column("summary", Text, nullable=False, server_default=""),
    Column("content_hash", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("created_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        ["intents.task_id", "intents.id"],
        ondelete="RESTRICT",
    ),
    UniqueConstraint("task_id", "path"),
)


credentials = Table(
    "credentials",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("label", String, nullable=False),
    Column("auth_type", String, nullable=False),
    Column("credentials_json", Text, nullable=False),
    Column("domains_json", Text, nullable=False, server_default="[]"),
    Column("notes", Text, nullable=False, server_default=""),
    Column("active", Boolean, nullable=False, server_default=text("1")),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("project_id", "label"),
)


vulnerabilities = Table(
    "vulnerabilities",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("intent_id", String),
    Column("title", Text, nullable=False),
    Column("type", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("description", Text, nullable=False),
    Column("prerequisites", Text, nullable=False, server_default=""),
    Column("impact", Text, nullable=False),
    Column("reproduction_steps_json", Text, nullable=False, server_default="[]"),
    Column("remediation", Text, nullable=False, server_default=""),
    Column("curl_command", Text, nullable=False, server_default=""),
    Column("evidence_json", Text, nullable=False, server_default="[]"),
    Column("affected_target", String, nullable=False, server_default=""),
    Column("status", String, nullable=False, server_default="待验证"),
    Column("source", String, nullable=False),
    Column("reported_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(
        ["task_id", "intent_id"],
        ["intents.task_id", "intents.id"],
        ondelete="RESTRICT",
    ),
)


external_report_attempts = Table(
    "external_report_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("task_id", String, nullable=False),
    Column("vulnerability_id", String, nullable=False),
    Column("platform", String, nullable=False),
    Column("resolved_project", String, nullable=False, server_default=""),
    Column("payload_json", Text, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error", Text, nullable=False, server_default=""),
    Column("response_json", Text, nullable=False, server_default="{}"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    ForeignKeyConstraint(
        ["task_id", "vulnerability_id"],
        ["vulnerabilities.task_id", "vulnerabilities.id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("status IN ('pending','success','failed')", name="status"),
)


audit_events = Table(
    "audit_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String),
    Column("task_id", String),
    Column("intent_id", String),
    Column("workspace_id", String),
    Column("worker", String, nullable=False, server_default=""),
    Column("event_type", String, nullable=False),
    Column("level", String, nullable=False, server_default="info"),
    Column("message", Text, nullable=False),
    Column("payload_json", Text, nullable=False, server_default="{}"),
    Column("created_at", String, nullable=False),
)
Index("ix_audit_events_task_created", audit_events.c.task_id, audit_events.c.created_at)


context_revisions = Table(
    "context_revisions",
    metadata,
    Column("task_id", String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("graph_path", Text, nullable=False),
    Column("todo_path", Text, nullable=False),
    Column("artifact_index_path", Text, nullable=False),
    Column("hints_path", Text, nullable=False),
    Column("knowledge_path", Text, nullable=False),
    Column("hash_manifest_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    PrimaryKeyConstraint("task_id", "revision"),
)
