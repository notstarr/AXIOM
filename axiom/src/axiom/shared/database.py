from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import Connection, DateTime, Engine, MetaData, Text, create_engine, event, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.types import TypeDecorator


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

AXIOM_SCHEMA = "axiom"
POSTGRESQL_ALEMBIC_BRANCH = "postgresql@head"

metadata = MetaData(schema=AXIOM_SCHEMA, naming_convention=NAMING_CONVENTION)

REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "artifacts",
        "archive_batches",
        "archive_batch_members",
        "axiom_tools",
        "audit_events",
        "context_revisions",
        "credential_refreshes",
        "credentials",
        "facts",
        "id_counters",
        "intent_sources",
        "intents",
        "projects",
        "reason_workspaces",
        "reason_runs",
        "reason_run_sources",
        "plan_runs",
        "plan_run_sources",
        "plan_workspaces",
        "runtime_settings",
        "task_hints",
        "task_goal_cycles",
        "tasks",
        "todos",
        "vulnerabilities",
        "vulnerability_artifacts",
        "vulnerability_review_runs",
        "mcp_servers",
        "mcp_tools",
        "project_knowledge",
        "goal_evaluations",
        "todo_sources",
        "workers",
        "worker_axiom_tool_bindings",
        "phase_capability_bindings",
        "llm_token_usage",
        "llm_token_usage_sources",
        "intent_interventions",
    }
)


class JSONText(TypeDecorator[str]):
    """Expose the historical JSON-string contract over PostgreSQL JSONB.

    AXIOM's service layer deliberately parses JSON at its module boundaries.
    Keeping that Python contract avoids a risky all-at-once repository rewrite,
    while PostgreSQL still stores and validates a real JSONB value.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            if isinstance(value, str):
                return json.loads(value)
            return value
        if isinstance(value, str):
            json.loads(value)
            return value
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: Any, dialect) -> str | None:
        if value is None:
            return None
        if dialect.name != "postgresql" and isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class UTCDateTimeText(TypeDecorator[str]):
    """Expose ISO-8601 strings while storing PostgreSQL TIMESTAMPTZ."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if dialect.name != "postgresql":
            if isinstance(value, datetime):
                return _format_utc(value)
            return str(value)
        if isinstance(value, datetime):
            parsed = value
        else:
            raw = str(value).strip()
            if not raw:
                return None
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def process_result_value(self, value: Any, dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _format_utc(value)
        return str(value)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def database_url_from_env() -> str:
    _load_postgresql_env_file()
    direct = os.getenv("AXIOM_DATABASE_URL", "").strip()
    if direct:
        return direct
    host = os.getenv("AXIOM_DB_HOST", "127.0.0.1")
    port = int(os.getenv("AXIOM_DB_PORT", "55432"))
    name = os.getenv("AXIOM_DB_NAME", "axiom")
    user = os.getenv("AXIOM_DB_USER", "axiom_app")
    password = os.getenv("AXIOM_DB_PASSWORD", "axiom-app-local")
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=name,
    ).render_as_string(hide_password=False)


def _load_postgresql_env_file() -> None:
    path_value = os.getenv("AXIOM_PG_ENV_FILE", "").strip()
    if not path_value:
        path_value = "~/.local/share/axiom-v3/postgresql.env"
    path = os.path.expanduser(path_value)
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key.startswith("AXIOM_"):
            os.environ.setdefault(key, value)


def create_database_engine(
    database_url: str | None = None,
    *,
    application_name: str = "axiom",
    pool_size: int = 10,
    max_overflow: int = 10,
) -> Engine:
    url = make_url(database_url or database_url_from_env())
    if url.get_backend_name() != "postgresql":
        raise ValueError("AXIOM runtime requires a PostgreSQL database URL")
    connect_args = {
        "connect_timeout": int(os.getenv("AXIOM_DB_CONNECT_TIMEOUT_SECONDS", "5")),
        "application_name": application_name,
    }
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=int(os.getenv("AXIOM_DB_POOL_RECYCLE_SECONDS", "1800")),
        connect_args=connect_args,
    )

    @event.listens_for(engine, "connect")
    def configure_postgresql(dbapi_connection, _connection_record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, false)",
                (f"{int(os.getenv('AXIOM_DB_LOCK_TIMEOUT_MS', '5000'))}ms",),
            )
            cursor.execute(
                "SELECT set_config('idle_in_transaction_session_timeout', %s, false)",
                (f"{int(os.getenv('AXIOM_DB_IDLE_TX_TIMEOUT_MS', '60000'))}ms",),
            )

    return engine


def schema_is_ready(engine: Engine) -> bool:
    tables = set(inspect(engine).get_table_names(schema=AXIOM_SCHEMA))
    return REQUIRED_TABLES.issubset(tables)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection


T = TypeVar("T")


def run_transaction(
    engine: Engine,
    operation: Callable[[Connection], T],
    *,
    attempts: int = 3,
) -> T:
    """Run an idempotent transactional closure with bounded PG retries."""

    for attempt in range(attempts):
        try:
            with engine.begin() as connection:
                return operation(connection)
        except DBAPIError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate not in {"40P01", "40001"} or attempt + 1 >= attempts:
                raise
            time.sleep((0.05 * (3**attempt)) + random.uniform(0, 0.025))
    raise RuntimeError("unreachable transaction retry state")
