from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, Integer, String, Table, Text

from axiom.shared.database import JSONText, UTCDateTimeText, metadata


workers = Table(
    "workers",
    metadata,
    Column("name", String, primary_key=True),
    Column("worker_type", String, nullable=False),
    Column("capabilities_json", JSONText, nullable=False),
    Column("max_running", Integer, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("config_json", JSONText, nullable=False),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("cooldown_until", UTCDateTimeText),
    # Circuit-breaker state.  ``cooldown_until`` alone is a bare timestamp with
    # no history: it cannot escalate a repeatedly failing backend, and it cannot
    # tell a success from "we never tried again".  These four carry that history
    # so the breaker can back off further on each trip and decay on its own once
    # the backend has been quiet long enough.
    Column("transient_failure_count", Integer, nullable=False, server_default="0"),
    Column("trip_count", Integer, nullable=False, server_default="0"),
    Column("last_failure_at", UTCDateTimeText),
    Column("last_failure_kind", Text, nullable=False, server_default=""),
    # Whether the gateway accepts OpenAI's ``developer`` message role.
    # NULL = not probed yet (Pi keeps its default), FALSE = confirmed refused
    # (Pi must fall back to ``system``), TRUE = confirmed accepted.
    Column("developer_role_supported", Boolean),
    Column("created_at", UTCDateTimeText, nullable=False),
    Column("updated_at", UTCDateTimeText, nullable=False),
    CheckConstraint("max_running > 0", name="max_running"),
)
