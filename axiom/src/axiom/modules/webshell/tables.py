from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.shared.database import JSONText, UTCDateTimeText, metadata


webshell_connections = Table(
    "webshell_connections",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("url", Text, nullable=False),
    # auto | posix | windows：auto 由首次探测判定后固化；决定 file 三件套拼哪族命令
    Column("family", String, nullable=False, default="auto"),
    # {"method","headers","body","query","wrap"}；命令占位符 {{cmd}}
    Column("request_json", JSONText, nullable=False),
    # {"mode":"raw|between|regex","start","end","pattern","group","decode":"none|base64"}
    Column("response_json", JSONText, nullable=False),
    Column("status", String, nullable=False, default="untested"),
    Column("last_check_at", UTCDateTimeText),
    Column("last_check_summary", Text, nullable=False, default=""),
    Column("remark", Text, nullable=False, default=""),
    Column("created_by", String, nullable=False),
    Column("source_intent_id", String),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    ForeignKeyConstraint(
        ["task_id", "source_intent_id"],
        [intents.c.task_id, intents.c.id],
        ondelete="SET NULL",
    ),
)
Index(
    "ix_webshell_connections_task_status",
    webshell_connections.c.task_id,
    webshell_connections.c.status,
)


webshell_exec_logs = Table(
    "webshell_exec_logs",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("connection_id", String, nullable=False),
    Column("command", Text, nullable=False),
    Column("output", Text, nullable=False, default=""),
    Column("ok", Boolean, nullable=False, default=False),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("actor", String, nullable=False),
    Column("intent_id", String),
    Column("created_at", UTCDateTimeText, nullable=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(
        ["task_id", "connection_id"],
        [webshell_connections.c.task_id, webshell_connections.c.id],
        ondelete="CASCADE",
    ),
)
Index(
    "ix_webshell_exec_logs_connection",
    webshell_exec_logs.c.task_id,
    webshell_exec_logs.c.connection_id,
)
