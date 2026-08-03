from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, MetaData, create_engine, event, inspect


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "artifacts",
        "archive_batches",
        "archive_batch_members",
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
        "runtime_settings",
        "task_hints",
        "tasks",
        "todos",
        "vulnerabilities",
        "vulnerability_artifacts",
        "mcp_servers",
        "mcp_tools",
        "external_report_attempts",
        "project_knowledge",
        "goal_evaluations",
        "todo_sources",
        "workers",
        "llm_token_usage",
        "llm_token_usage_sources",
    }
)


def create_sqlite_engine(path: str | Path) -> Engine:
    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA busy_timeout = 30000")
        finally:
            cursor.close()

    return engine


def schema_is_ready(engine: Engine) -> bool:
    tables = set(inspect(engine).get_table_names())
    return REQUIRED_TABLES.issubset(tables)


@contextmanager
def immediate_transaction(engine: Engine) -> Iterator[Connection]:
    """Serialize a SQLite read-check-write critical section.

    Intent capacity checks and claims must be one atomic operation.  SQLite's
    default deferred transaction permits two dispatchers to observe the same
    free slot before either writes.  ``BEGIN IMMEDIATE`` acquires the reserved
    write lock before those checks while still allowing concurrent readers.
    """

    connection = engine.connect()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
