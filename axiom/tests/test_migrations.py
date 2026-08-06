from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from axiom.cli import alembic_config, database_heads, migration_head
from axiom.migration import sqlite_to_postgresql as importer
from axiom.shared import model_registry as _model_registry  # noqa: F401
from axiom.shared.database import (
    AXIOM_SCHEMA,
    POSTGRESQL_ALEMBIC_BRANCH,
    create_database_engine,
    metadata,
    schema_is_ready,
)


ADMIN_URL = os.getenv(
    "AXIOM_TEST_ADMIN_URL",
    "postgresql+psycopg://axiom_bootstrap:axiom-bootstrap-test@127.0.0.1:55433/postgres",
)
MIGRATOR_URL = os.getenv(
    "AXIOM_TEST_MIGRATOR_URL",
    "postgresql+psycopg://axiom_migrator:axiom-migrator-test@127.0.0.1:55433/axiom_test",
)
APP_URL = os.getenv(
    "AXIOM_TEST_DATABASE_URL",
    "postgresql+psycopg://axiom_app:axiom-app-test@127.0.0.1:55433/axiom_test",
)
SCHEMA_FIXTURE = Path(__file__).with_name("fixtures") / "sqlite_20260805_0035_schema.sql"


@pytest.fixture()
def isolated_postgresql_database() -> Iterator[tuple[str, str]]:
    database = f"axiom_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    quoted = '"' + database.replace('"', '""') + '"'
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE {quoted} OWNER axiom_owner")
            conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {quoted} TO axiom_migrator, axiom_app")
        migrator_url = make_url(MIGRATOR_URL).set(database=database).render_as_string(
            hide_password=False
        )
        app_url = make_url(APP_URL).set(database=database).render_as_string(hide_password=False)
        yield migrator_url, app_url
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
        admin.dispose()


def _upgrade(migrator_url: str) -> None:
    command.upgrade(alembic_config(migrator_url), POSTGRESQL_ALEMBIC_BRANCH)


def _create_sqlite_source(path: Path, *, revision: str = importer.LEGACY_SQLITE_REVISION) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_FIXTURE.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (revision,),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_minimal_source_data(path: Path, *, counter_next: int = 1) -> None:
    connection = sqlite3.connect(path)
    try:
        now = "2026-08-05T12:34:56Z"
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO projects (id, title, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("prj_001", "migration", "", "active", now, now),
        )
        connection.execute(
            "INSERT INTO workers "
            "(name, worker_type, capabilities_json, max_running, priority, config_json, "
            "disabled, cooldown_until, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "worker-pg",
                "pi",
                json.dumps(["reason", "explore"]),
                2,
                0,
                json.dumps({"nested": {"b": 2, "a": 1}}),
                False,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO id_counters (scope, kind, next_value) VALUES (?, ?, ?)",
            ("global", "project", counter_next),
        )
        connection.commit()
    finally:
        connection.close()


def _business_counts(app_url: str) -> dict[str, int]:
    engine = create_database_engine(app_url, application_name="axiom-migration-test")
    try:
        with engine.connect() as conn:
            return {
                table.name: int(
                    conn.execute(text(f'SELECT count(*) FROM "{AXIOM_SCHEMA}"."{table.name}"'))
                    .scalar_one()
                )
                for table in metadata.sorted_tables
            }
    finally:
        engine.dispose()


def test_postgresql_baseline_upgrade_has_no_metadata_drift_and_native_types(
    isolated_postgresql_database,
) -> None:
    migrator_url, app_url = isolated_postgresql_database
    _upgrade(migrator_url)
    engine = create_database_engine(app_url, application_name="axiom-migration-test")
    try:
        assert schema_is_ready(engine)
        assert database_heads(engine) == {migration_head(alembic_config(migrator_url))}
        inspector = inspect(engine)
        assert len(inspector.get_table_names(schema=AXIOM_SCHEMA)) == len(metadata.sorted_tables) + 1
        task_columns = {
            item["name"]: item
            for item in inspector.get_columns("tasks", schema=AXIOM_SCHEMA)
        }
        assert str(task_columns["scope_json"]["type"]).upper() == "JSONB"
        assert task_columns["origin_host"]["nullable"] is True
        assert "TIMESTAMP" in str(task_columns["created_at"]["type"]).upper()
        assert task_columns["created_at"]["type"].timezone is True
        assert str(task_columns["guard_enabled"]["type"]).upper() == "BOOLEAN"

        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_schemas": True,
                    "version_table_schema": AXIOM_SCHEMA,
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        type_ != "table" or name != "alembic_version"
                    ),
                },
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


def test_postgresql_baseline_downgrades_and_reupgrades(isolated_postgresql_database) -> None:
    migrator_url, app_url = isolated_postgresql_database
    config = alembic_config(migrator_url)
    command.upgrade(config, POSTGRESQL_ALEMBIC_BRANCH)
    command.downgrade(config, "base")

    probe = create_engine(app_url, pool_pre_ping=True)
    try:
        assert inspect(probe).get_table_names(schema=AXIOM_SCHEMA) == ["alembic_version"]
    finally:
        probe.dispose()

    command.upgrade(config, POSTGRESQL_ALEMBIC_BRANCH)
    engine = create_database_engine(app_url, application_name="axiom-migration-test")
    try:
        assert schema_is_ready(engine)
    finally:
        engine.dispose()


def test_origin_scope_decoupling_migration_normalizes_disabled_scope_and_rolls_back(
    isolated_postgresql_database,
) -> None:
    migrator_url, app_url = isolated_postgresql_database
    config = alembic_config(migrator_url)
    command.upgrade(config, POSTGRESQL_ALEMBIC_BRANCH)
    command.downgrade(config, "20260805_pg0001")

    engine = create_database_engine(app_url, application_name="axiom-migration-test")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'INSERT INTO "{AXIOM_SCHEMA}".projects '
                    "(id, title, description, status, created_at, updated_at) "
                    "VALUES ('prj_scope', 'scope', '', 'active', now(), now())"
                )
            )
            for task_id, scope_json in [
                (
                    "t_disabled",
                    {"enabled": False, "targets": ["dormant.example.com"], "exclude": []},
                ),
                (
                    "t_enabled",
                    {
                        "enabled": True,
                        "targets": ["allowed.example.com"],
                        "exclude": ["blocked.example.com"],
                    },
                ),
            ]:
                conn.execute(
                    text(
                        f'INSERT INTO "{AXIOM_SCHEMA}".tasks '
                        "(id, project_id, title, origin_host, origin_input, goal, status, phase, "
                        "scope_json, worker_pool_json, guard_enabled, external_report_enabled, "
                        "legacy_readonly, archived, created_at, updated_at) "
                        "VALUES (:id, 'prj_scope', 'legacy task', 'legacy.example.com', "
                        "'legacy.example.com', 'goal', 'stopped', 'none', CAST(:scope AS jsonb), "
                        "CAST('[]' AS jsonb), false, false, false, false, now(), now())"
                    ),
                    {"id": task_id, "scope": json.dumps(scope_json)},
                )
    finally:
        engine.dispose()

    command.upgrade(config, POSTGRESQL_ALEMBIC_BRANCH)
    engine = create_database_engine(app_url, application_name="axiom-migration-test")
    try:
        inspector = inspect(engine)
        origin_host = next(
            item
            for item in inspector.get_columns("tasks", schema=AXIOM_SCHEMA)
            if item["name"] == "origin_host"
        )
        assert origin_host["nullable"] is True
        with engine.begin() as conn:
            scopes = dict(
                conn.execute(
                    text(
                        f'SELECT id, scope_json FROM "{AXIOM_SCHEMA}".tasks '
                        "WHERE id IN ('t_disabled', 't_enabled') ORDER BY id"
                    )
                ).all()
            )
            assert scopes["t_disabled"] == {
                "enabled": False,
                "targets": [],
                "exclude": [],
            }
            assert scopes["t_enabled"] == {
                "enabled": True,
                "targets": ["allowed.example.com"],
                "exclude": ["blocked.example.com"],
            }
            conn.execute(
                text(
                    f'INSERT INTO "{AXIOM_SCHEMA}".tasks '
                    "(id, project_id, title, origin_host, origin_input, goal, status, phase, "
                    "scope_json, worker_pool_json, guard_enabled, external_report_enabled, "
                    "legacy_readonly, archived, created_at, updated_at) "
                    "VALUES ('t_natural', 'prj_scope', 'natural', NULL, '微信小程序测试商城', "
                    "'goal', 'stopped', 'none', CAST(:natural_scope AS jsonb), "
                    "CAST('[]' AS jsonb), false, false, false, "
                    "false, now(), now())"
                ),
                {
                    "natural_scope": json.dumps(
                        {"enabled": False, "targets": [], "exclude": []}
                    )
                },
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260805_pg0001")
    engine = create_database_engine(app_url, application_name="axiom-migration-test")
    try:
        origin_host = next(
            item
            for item in inspect(engine).get_columns("tasks", schema=AXIOM_SCHEMA)
            if item["name"] == "origin_host"
        )
        assert origin_host["nullable"] is False
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    f'SELECT origin_host FROM "{AXIOM_SCHEMA}".tasks '
                    "WHERE id = 't_natural'"
                )
            ).scalar_one() == "nonnetwork.invalid"
    finally:
        engine.dispose()

    command.upgrade(config, POSTGRESQL_ALEMBIC_BRANCH)


def test_application_role_cannot_change_schema(isolated_postgresql_database) -> None:
    migrator_url, app_url = isolated_postgresql_database
    _upgrade(migrator_url)
    engine = create_database_engine(app_url, application_name="axiom-permission-test")
    try:
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.exec_driver_sql(f'CREATE TABLE "{AXIOM_SCHEMA}"."forbidden" (id int)')
    finally:
        engine.dispose()


def test_sqlite_importer_migrates_json_timestamps_and_synchronizes_counters(
    isolated_postgresql_database,
    tmp_path,
) -> None:
    migrator_url, app_url = isolated_postgresql_database
    _upgrade(migrator_url)
    source = tmp_path / "source.db"
    _create_sqlite_source(source)
    _insert_minimal_source_data(source, counter_next=1)

    report = importer.migrate_sqlite_to_postgresql(source, app_url, data_dir=tmp_path)

    assert report.matches is True
    assert len(report.tables) == len(metadata.sorted_tables)
    project_counter = next(
        item for item in report.counters if item.scope == "global" and item.kind == "project"
    )
    assert project_counter.source_next_value == 1
    assert project_counter.required_next_value == 2
    assert project_counter.target_next_value == 2
    assert project_counter.adjusted is True

    engine = create_database_engine(app_url, application_name="axiom-import-test")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f'SELECT capabilities_json, config_json, created_at, disabled '
                    f'FROM "{AXIOM_SCHEMA}".workers WHERE name = :name'
                ),
                {"name": "worker-pg"},
            ).mappings().one()
            assert row["capabilities_json"] == ["reason", "explore"]
            assert row["config_json"] == {"nested": {"a": 1, "b": 2}}
            assert row["created_at"].tzinfo is not None
            assert row["disabled"] is False
            assert conn.execute(
                text(
                    f'SELECT next_value FROM "{AXIOM_SCHEMA}".id_counters '
                    "WHERE scope = 'global' AND kind = 'project'"
                )
            ).scalar_one() == 2
    finally:
        engine.dispose()


def test_sqlite_importer_verification_failure_rolls_back_every_business_table(
    isolated_postgresql_database,
    tmp_path,
    monkeypatch,
) -> None:
    migrator_url, app_url = isolated_postgresql_database
    _upgrade(migrator_url)
    source = tmp_path / "source.db"
    _create_sqlite_source(source)
    _insert_minimal_source_data(source)

    original = importer._rows_hash
    calls = 0
    source_hash_calls = len(metadata.sorted_tables)

    def mismatching_hash(table, rows):
        nonlocal calls
        calls += 1
        value = original(table, rows)
        if calls > source_hash_calls and table.name == "projects":
            return "0" * 64
        return value

    monkeypatch.setattr(importer, "_rows_hash", mismatching_hash)
    with pytest.raises(RuntimeError, match="transaction rolled back"):
        importer.migrate_sqlite_to_postgresql(source, app_url)

    assert all(count == 0 for count in _business_counts(app_url).values())


def test_sqlite_importer_rejects_nonempty_target(
    isolated_postgresql_database,
    tmp_path,
) -> None:
    migrator_url, app_url = isolated_postgresql_database
    _upgrade(migrator_url)
    source = tmp_path / "source.db"
    _create_sqlite_source(source)
    _insert_minimal_source_data(source)

    engine = create_database_engine(app_url, application_name="axiom-import-test")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'INSERT INTO "{AXIOM_SCHEMA}".projects '
                    "(id, title, description, status, created_at, updated_at) "
                    "VALUES ('prj_existing', 'existing', '', 'active', now(), now())"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(ValueError, match="target is not empty"):
        importer.migrate_sqlite_to_postgresql(source, app_url)


def test_sqlite_importer_rejects_wrong_revision_invalid_json_and_invalid_time(
    isolated_postgresql_database,
    tmp_path,
) -> None:
    migrator_url, app_url = isolated_postgresql_database
    _upgrade(migrator_url)

    wrong_revision = tmp_path / "wrong-revision.db"
    _create_sqlite_source(wrong_revision, revision="old")
    with pytest.raises(ValueError, match="revision must be"):
        importer.migrate_sqlite_to_postgresql(wrong_revision, app_url)

    invalid_json = tmp_path / "invalid-json.db"
    _create_sqlite_source(invalid_json)
    connection = sqlite3.connect(invalid_json)
    try:
        now = "2026-08-05T12:34:56Z"
        connection.execute(
            "INSERT INTO workers "
            "(name, worker_type, capabilities_json, max_running, priority, config_json, "
            "disabled, cooldown_until, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("bad-json", "pi", "[", 1, 0, "{}", 0, None, now, now),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(json.JSONDecodeError):
        importer.migrate_sqlite_to_postgresql(invalid_json, app_url)

    invalid_time = tmp_path / "invalid-time.db"
    _create_sqlite_source(invalid_time)
    connection = sqlite3.connect(invalid_time)
    try:
        connection.execute(
            "INSERT INTO projects (id, title, description, status, created_at, updated_at) "
            "VALUES ('prj_001', 'bad time', '', 'active', 'not-a-time', 'not-a-time')"
        )
        connection.execute(
            "INSERT INTO id_counters (scope, kind, next_value) VALUES ('global', 'project', 2)"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="Invalid isoformat"):
        importer.migrate_sqlite_to_postgresql(invalid_time, app_url)
