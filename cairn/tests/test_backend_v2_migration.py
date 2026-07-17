from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select, text

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import (
    artifacts,
    credentials,
    facts,
    id_counters,
    intents,
    project_knowledge,
    projects,
    task_hints,
    tasks,
    vulnerabilities,
)
from axiom.migration.v2 import migrate_legacy_database


def create_legacy_db(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE project_groups (id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT, created_at TEXT);
            CREATE TABLE projects (id TEXT PRIMARY KEY, parent_project_id TEXT, title TEXT, status TEXT, bootstrap_enabled INTEGER, created_at TEXT, kk_report_enabled INTEGER, worker_pool TEXT, guard_enabled INTEGER, task_scope_enabled INTEGER, task_scope_json TEXT);
            CREATE TABLE facts (id TEXT, project_id TEXT, description TEXT, archived INTEGER);
            CREATE TABLE intents (id TEXT, project_id TEXT, to_fact_id TEXT, description TEXT, creator TEXT, worker TEXT, last_heartbeat_at TEXT, created_at TEXT, concluded_at TEXT, archived INTEGER, execute_timeout_seconds INTEGER, conclude_timeout_seconds INTEGER, worker_pool TEXT);
            CREATE TABLE intent_sources (intent_id TEXT, project_id TEXT, fact_id TEXT);
            CREATE TABLE hints (id TEXT, project_id TEXT, content TEXT, creator TEXT, created_at TEXT);
            CREATE TABLE blackboard_items (id TEXT, project_id TEXT, group_id TEXT, title TEXT, content TEXT, hint TEXT, creator TEXT, updated_by TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE vulns (id TEXT, project_id TEXT, title TEXT, type TEXT, severity TEXT, description TEXT, prerequisites TEXT, impact TEXT, evidence_json TEXT, reproduction_steps_json TEXT, recommendation TEXT, affected_scope TEXT, status TEXT, source TEXT, reported_by TEXT, created_at TEXT);
            CREATE TABLE credentials (id TEXT, group_id TEXT, label TEXT, auth_type TEXT, credentials_json TEXT, domains_json TEXT, active INTEGER, captured_at TEXT);
            CREATE TABLE assets (id INTEGER, project_id TEXT, group_id TEXT, backend_domain TEXT, api_path TEXT, api_method TEXT, api_purpose TEXT, test_status TEXT, discovered_at TEXT);
            """
        )
        now = "2026-07-17T00:00:00Z"
        conn.execute("INSERT INTO project_groups VALUES (?,?,?,?,?)", ("pg_001", "Legacy", "desc", "active", now))
        conn.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("proj_093", "pg_001", "Task 93", "completed", 1, now, 1, "[]", 0, 1, '{"targets":["example.com"],"exclude":[],"notes":""}'),
        )
        conn.executemany(
            "INSERT INTO facts VALUES (?,?,?,?)",
            [("origin", "proj_093", "https://example.com", 0), ("goal", "proj_093", "finish", 0), ("f001", "proj_093", "confirmed", 0)],
        )
        conn.execute(
            "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("i001", "proj_093", "f001", "bootstrap", "dispatcher.bootstrap", "worker-a", now, now, now, 0, 300, 90, "[]"),
        )
        conn.execute("INSERT INTO intent_sources VALUES (?,?,?)", ("i001", "proj_093", "origin"))
        conn.execute("INSERT INTO hints VALUES (?,?,?,?,?)", ("h001", "proj_093", "legacy hint", "human", now))
        conn.execute("INSERT INTO blackboard_items VALUES (?,?,?,?,?,?,?,?,?,?)", ("b001", None, "pg_001", "Auth", "JWT", "", "human", "human", now, now))
        conn.execute(
            "INSERT INTO vulns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("v001", "proj_093", "IDOR", "越权访问", "高危", "desc", "login", "impact", "[]", '["step"]', "fix", "example.com", "待验证", "explore", "worker-a", now),
        )
        conn.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?)", ("c001", "pg_001", "jwt", "jwt", '{"Authorization":"Bearer x"}', '["example.com"]', 1, now))
        conn.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)", (1, "proj_093", "pg_001", "example.com", "/api/users", "GET", "users", "untested", now))


def test_legacy_migration_preserves_history_as_read_only_and_archives_assets(tmp_path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "v2.db"
    data_dir = tmp_path / "data"
    create_legacy_db(source)

    report = migrate_legacy_database(source, target, data_dir=data_dir)
    engine = create_sqlite_engine(target)
    with engine.connect() as conn:
        project = conn.execute(select(projects)).mappings().one()
        task = conn.execute(select(tasks)).mappings().one()
        assert project["id"] == "prj_001"
        assert task["id"] == "t_093"
        assert task["legacy_readonly"] is True
        assert task["status"] == "completed"
        assert conn.execute(select(facts)).mappings().all()
        assert conn.execute(select(intents)).mappings().one()["status"] == "completed"
        assert conn.execute(select(task_hints)).mappings().one()["content"] == "legacy hint"
        assert conn.execute(select(project_knowledge)).mappings().one()["content"] == "JWT"
        assert conn.execute(select(vulnerabilities)).mappings().one()["severity"] == "high"
        assert json.loads(conn.execute(select(credentials)).mappings().one()["credentials_json"])["Authorization"] == "Bearer x"
        artifact = conn.execute(select(artifacts)).mappings().one()
        assert artifact["artifact_type"] == "legacy_asset_inventory"
        counters = {
            (row["scope"], row["kind"]): row["next_value"]
            for row in conn.execute(select(id_counters)).mappings()
        }
        assert counters[("global", "project")] == 2
        assert counters[("global", "task")] == 94
        assert counters[("global", "credential")] == 1
        assert counters[("prj_001", "knowledge")] == 2
        assert counters[("t_093", "hint")] == 2
        assert counters[("t_093", "artifact")] == 2
        assert counters[("t_093", "vulnerability")] == 2
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "51313e2f15db"

    assert report.projects == 1
    assert report.tasks == 1
    assert report.assets == 1


def test_migration_handles_colliding_ids_orphans_and_broken_references(tmp_path) -> None:
    source = tmp_path / "legacy.db"
    target = tmp_path / "v2.db"
    with sqlite3.connect(source) as conn:
        conn.executescript(
            """
            CREATE TABLE project_groups (id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT, created_at TEXT);
            CREATE TABLE projects (id TEXT PRIMARY KEY, parent_project_id TEXT, title TEXT, status TEXT, bootstrap_enabled INTEGER, created_at TEXT, kk_report_enabled INTEGER, worker_pool TEXT, guard_enabled INTEGER, task_scope_enabled INTEGER, task_scope_json TEXT);
            CREATE TABLE facts (id TEXT, project_id TEXT, description TEXT, archived INTEGER);
            CREATE TABLE intents (id TEXT, project_id TEXT, to_fact_id TEXT, description TEXT, creator TEXT, worker TEXT, last_heartbeat_at TEXT, created_at TEXT, concluded_at TEXT, archived INTEGER, execute_timeout_seconds INTEGER, conclude_timeout_seconds INTEGER, worker_pool TEXT);
            CREATE TABLE intent_sources (intent_id TEXT, project_id TEXT, fact_id TEXT);
            """
        )
        now = "2026-07-17T00:00:00Z"
        conn.executemany(
            "INSERT INTO project_groups VALUES (?,?,?,?,?)",
            [("pg_001", "A", "", "active", now), ("tenant1", "B", "", "active", now)],
        )
        conn.executemany(
            "INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("proj_093", "pg_001", "A", "running", 1, now, 0, "[]", 0, 1, "{}"),
                ("task93", "tenant1", "B", "running", 1, now, 0, "[]", 0, 1, "{}"),
                ("orphan", "pg_999", "C", "running", 1, now, 0, "[]", 0, 1, "{}"),
            ],
        )
        conn.executemany(
            "INSERT INTO facts VALUES (?,?,?,?)",
            [
                ("origin", "proj_093", "https://example.com", 0),
                ("goal", "proj_093", "goal", 0),
                ("origin", "task93", "not a valid origin", 0),
                ("goal", "task93", "goal", 0),
                ("origin", "orphan", "10.0.0.1", 0),
                ("goal", "orphan", "goal", 0),
            ],
        )
        conn.execute(
            "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("i001", "proj_093", "missing", "broken", "legacy", None, None, now, None, 0, None, None, "[]"),
        )
        conn.executemany(
            "INSERT INTO intent_sources VALUES (?,?,?)",
            [("i001", "proj_093", "origin"), ("i999", "proj_093", "origin")],
        )

    report = migrate_legacy_database(source, target, data_dir=tmp_path / "data")
    engine = create_sqlite_engine(target)
    with engine.connect() as conn:
        migrated_projects = conn.execute(select(projects)).mappings().all()
        migrated_tasks = conn.execute(select(tasks)).mappings().all()
        assert len(migrated_projects) == 3
        assert len({row["id"] for row in migrated_projects}) == 3
        assert len({row["id"] for row in migrated_tasks}) == 3
        assert conn.execute(select(intents.c.to_fact_id)).scalar_one() is None
        invalid = next(row for row in migrated_tasks if row["title"] == "B")
        assert invalid["origin_host"] == "legacy.invalid"
        assert invalid["scope_enabled"] is False
        assert all(row["legacy_readonly"] for row in migrated_tasks)
    assert any("reference cleared" in warning for warning in report.warnings)
    assert any("Orphan intent source skipped" in warning for warning in report.warnings)
    assert any("Invalid legacy origin" in warning for warning in report.warnings)
