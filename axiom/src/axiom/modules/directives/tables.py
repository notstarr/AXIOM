"""Tables for human-issued worker directives.

Two tables, two different jobs:

* ``worker_directives`` is the editable inventory. Persistent directives live here
  and are re-read on every execution input; one-shot directives live here until
  they are delivered.
* ``directive_deliveries`` is an append-only ledger. One-shot instructions are
  frozen to an owner (a ReasonRun or an Explore Intent) plus a lease at claim
  time, so ``/input`` can stay a pure, idempotent read even when the dispatcher
  retries the same lease or a stale lease is re-claimed with a new id.

There is deliberately no FK from the ledger to the inventory: a reopened or
archived directive must still leave its delivery history readable.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

from axiom.modules.tasks.tables import tasks
from axiom.shared.database import UTCDateTimeText, metadata


_PERSISTENT_SCOPES = "'reason_all','explore_all','all_workers'"
_ONCE_SCOPES = "'reason_next','explore_one'"


worker_directives = Table(
    "worker_directives",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("content", Text, nullable=False),
    Column("creator", String, nullable=False),
    Column("scope", String, nullable=False),
    Column("lifetime", String, nullable=False),
    Column("target_intent_id", String),
    Column("status", String, nullable=False),
    Column("client_message_id", String),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText),
    Column("delivered_at", UTCDateTimeText),
    Column("delivered_run_id", String),
    Column("disabled_at", UTCDateTimeText),
    Column("expires_at", UTCDateTimeText),
    Column("archived", Boolean, nullable=False, default=False),
    PrimaryKeyConstraint("task_id", "id"),
    ForeignKeyConstraint(["task_id"], [tasks.c.id], ondelete="CASCADE"),
    CheckConstraint(
        "scope IN ('reason_all','reason_next','explore_all','explore_one','all_workers')",
        name="scope",
    ),
    CheckConstraint("lifetime IN ('persistent','once')", name="lifetime"),
    CheckConstraint(
        "status IN ('pending','active','delivered','disabled','expired')",
        name="status",
    ),
    CheckConstraint(
        "(scope = 'explore_one' AND target_intent_id IS NOT NULL) "
        "OR (scope <> 'explore_one' AND target_intent_id IS NULL)",
        name="target",
    ),
    CheckConstraint(
        f"(lifetime = 'persistent' AND scope IN ({_PERSISTENT_SCOPES})) "
        f"OR (lifetime = 'once' AND scope IN ({_ONCE_SCOPES}))",
        name="consistency",
    ),
)
Index(
    "uq_worker_directives_client",
    worker_directives.c.task_id,
    worker_directives.c.client_message_id,
    unique=True,
)
Index(
    "ix_worker_directives_task_scope_status",
    worker_directives.c.task_id,
    worker_directives.c.scope,
    worker_directives.c.status,
)
Index(
    "ix_worker_directives_task_status_created",
    worker_directives.c.task_id,
    worker_directives.c.status,
    worker_directives.c.created_at,
    worker_directives.c.id,
)
Index(
    "ix_worker_directives_task_target_status",
    worker_directives.c.task_id,
    worker_directives.c.target_intent_id,
    worker_directives.c.status,
)


directive_deliveries = Table(
    "directive_deliveries",
    metadata,
    Column("id", String, nullable=False),
    Column("task_id", String, nullable=False),
    Column("directive_id", String, nullable=False),
    Column("delivery_kind", String, nullable=False),
    Column("reason_run_id", String),
    Column("intent_id", String),
    Column("lease_id", String),
    Column("status", String, nullable=False),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("delivered_at", UTCDateTimeText),
    Column("reopened_at", UTCDateTimeText),
    PrimaryKeyConstraint("task_id", "id"),
    CheckConstraint("delivery_kind IN ('reason_next','explore_one')", name="delivery_kind"),
    CheckConstraint(
        "status IN ('queued','delivered','reopened')", name="status"
    ),
)
Index(
    "uq_directive_deliveries_lease",
    directive_deliveries.c.directive_id,
    directive_deliveries.c.lease_id,
    unique=True,
)
Index(
    "uq_directive_deliveries_reason",
    directive_deliveries.c.delivery_kind,
    directive_deliveries.c.reason_run_id,
    directive_deliveries.c.directive_id,
    unique=True,
)
Index(
    "ix_directive_deliveries_lease",
    directive_deliveries.c.lease_id,
    directive_deliveries.c.status,
)
Index(
    "ix_directive_deliveries_run",
    directive_deliveries.c.task_id,
    directive_deliveries.c.reason_run_id,
)
Index(
    "ix_directive_deliveries_intent",
    directive_deliveries.c.task_id,
    directive_deliveries.c.intent_id,
    directive_deliveries.c.status,
)
