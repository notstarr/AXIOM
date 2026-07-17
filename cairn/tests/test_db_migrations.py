from __future__ import annotations

import json
import sqlite3

from axiom.server import db


def test_fresh_schema_keeps_scope_only_on_tasks(tmp_path, monkeypatch) -> None:
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        group_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_groups)")}
        project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}

    assert "scope_json" not in group_columns
    assert {"task_scope_enabled", "task_scope_json", "credential_headers"} <= project_columns


def test_legacy_project_group_scope_is_migrated_then_cleared(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy-scope.db"
    legacy_scope = json.dumps(
        {"targets": ["api.example.com"], "exclude": ["blocked.example.com"], "notes": "legacy"}
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE project_groups (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO project_groups VALUES ('pg_001', 'legacy', '', ?, 'active', '2026-01-01T00:00:00Z')",
            (legacy_scope,),
        )
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                parent_project_id TEXT,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
                task_scope_enabled INTEGER NOT NULL DEFAULT 0,
                task_scope_json TEXT NOT NULL DEFAULT '{"targets":[],"exclude":[],"notes":""}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, parent_project_id, title, created_at) VALUES ('proj_001', 'pg_001', 'legacy', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        project = conn.execute(
            "SELECT task_scope_enabled, task_scope_json FROM projects WHERE id = 'proj_001'"
        ).fetchone()
        group = conn.execute("SELECT scope_json FROM project_groups WHERE id = 'pg_001'").fetchone()

    assert project["task_scope_enabled"] == 1
    assert json.loads(project["task_scope_json"])["targets"] == ["api.example.com"]
    assert group["scope_json"] == db.DEFAULT_TASK_SCOPE_JSON


def test_configure_adds_bootstrap_enabled_to_legacy_projects_table(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'legacy', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT bootstrap_enabled, kk_report_enabled FROM projects WHERE id = 'proj_001'"
        ).fetchone()
    assert row["bootstrap_enabled"] == 1
    assert row["kk_report_enabled"] == 1


def test_configure_maps_disabled_bootstrap_mode_to_false(tmp_path, monkeypatch) -> None:
    path = tmp_path / "intermediate.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                bootstrap_mode TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_001', 'disabled', 'disabled', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_002', 'enabled', 'enabled', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, bootstrap_enabled, kk_report_enabled FROM projects ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["bootstrap_enabled"], row["kk_report_enabled"]) for row in rows] == [
        ("proj_001", 0, 1),
        ("proj_002", 1, 1),
    ]
