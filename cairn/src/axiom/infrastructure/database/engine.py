from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event


def create_sqlite_engine(path: str | Path) -> Engine:
    """Create the V2 SQLite engine with AXIOM's required durability settings."""

    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 5000")
            cursor.execute("PRAGMA journal_mode = WAL")
        finally:
            cursor.close()

    return engine
