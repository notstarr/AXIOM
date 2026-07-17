from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata, projects, tasks


EXPECTED_CORE_TABLES = {
    "id_counters",
    "projects",
    "tasks",
    "facts",
    "intents",
    "intent_sources",
    "task_hints",
    "project_knowledge",
    "todos",
    "todo_sources",
    "reason_workspaces",
    "artifacts",
    "credentials",
    "vulnerabilities",
    "external_report_attempts",
    "audit_events",
    "context_revisions",
}


def test_v2_metadata_creates_expected_core_tables(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)

    assert EXPECTED_CORE_TABLES <= set(inspect(engine).get_table_names())


def test_sqlite_engine_enables_wal_foreign_keys_and_busy_timeout(tmp_path) -> None:
    db_path = tmp_path / "axiom-v2.db"
    engine = create_sqlite_engine(db_path)
    metadata.create_all(engine)

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() >= 5000


def test_task_requires_existing_project(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                tasks.insert().values(
                    id="t_001",
                    project_id="prj_missing",
                    title="task",
                    origin_host="example.com",
                    origin_input="example.com",
                    goal="finish",
                    created_at="2026-07-17T00:00:00Z",
                    updated_at="2026-07-17T00:00:00Z",
                )
            )

    with engine.begin() as conn:
        conn.execute(
            projects.insert().values(
                id="prj_001",
                title="project",
                created_at="2026-07-17T00:00:00Z",
                updated_at="2026-07-17T00:00:00Z",
            )
        )
        conn.execute(
            tasks.insert().values(
                id="t_001",
                project_id="prj_001",
                title="task",
                origin_host="example.com",
                origin_input="example.com",
                goal="finish",
                created_at="2026-07-17T00:00:00Z",
                updated_at="2026-07-17T00:00:00Z",
            )
        )

    with sqlite3.connect(db_path := tmp_path / "axiom-v2.db") as raw:
        assert raw.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_alembic_upgrades_empty_database_to_v2_head(tmp_path) -> None:
    cairn_dir = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "migrated.db"
    config = Config(str(cairn_dir / "alembic.ini"))
    config.set_main_option("script_location", str(cairn_dir / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(config, "head")

    engine = create_sqlite_engine(db_path)
    assert EXPECTED_CORE_TABLES <= set(inspect(engine).get_table_names())
