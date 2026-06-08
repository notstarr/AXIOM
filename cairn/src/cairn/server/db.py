from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    bootstrap_worker_count INTEGER NOT NULL DEFAULT 1,
    explore_worker_count INTEGER NOT NULL DEFAULT 2,
    reason_worker_count INTEGER NOT NULL DEFAULT 1,
    openai_base_url TEXT NOT NULL DEFAULT '',
    openai_model TEXT NOT NULL DEFAULT '',
    proxy_url TEXT NOT NULL DEFAULT '',
    proxy_bootstrap_enabled INTEGER NOT NULL DEFAULT 0,
    proxy_explore_enabled INTEGER NOT NULL DEFAULT 0,
    domain_blacklist_json TEXT NOT NULL DEFAULT '[]'
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS project_groups (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '{"targets":[],"exclude":[],"notes":""}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    parent_project_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);
INSERT OR IGNORE INTO counters (name, value) VALUES ('project_group', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS provider_keys (
    provider TEXT PRIMARY KEY,
    key_ciphertext TEXT NOT NULL,
    key_hint TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    worker TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_project_id_id ON task_events(project_id, id);

CREATE TABLE IF NOT EXISTS vulns (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    description TEXT NOT NULL,
    impact TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    reproduction_steps_json TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT NOT NULL,
    affected_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    source TEXT NOT NULL DEFAULT 'reason',
    reported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_vulns_project_id_created_at ON vulns(project_id, created_at);

CREATE TABLE IF NOT EXISTS blackboard_items (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT 'medium',
    related_fact_id TEXT,
    related_intent_id TEXT,
    related_vuln_id TEXT,
    creator TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_blackboard_items_project_id_updated_at ON blackboard_items(project_id, updated_at);

CREATE TABLE IF NOT EXISTS tool_executions (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,
    argv_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    returncode INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    project_group_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_executions_created_at ON tool_executions(created_at);
CREATE INDEX IF NOT EXISTS idx_tool_executions_project_id ON tool_executions(project_id);

CREATE TABLE IF NOT EXISTS pi_tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'custom',
    schema_json TEXT NOT NULL DEFAULT '{}',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pi_tools_enabled_name ON pi_tools(enabled, name);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_settings_columns(conn)
        _ensure_project_group_schema(conn)
        _ensure_project_columns(conn)
        _ensure_blackboard_columns(conn)
        _ensure_pi_tools_schema(conn)


def current_db_path() -> Path:
    return _db_path or DEFAULT_DB


def _ensure_settings_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "bootstrap_worker_count" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN bootstrap_worker_count INTEGER NOT NULL DEFAULT 1")
    if "explore_worker_count" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN explore_worker_count INTEGER NOT NULL DEFAULT 2")
    if "reason_worker_count" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN reason_worker_count INTEGER NOT NULL DEFAULT 1")
    if "openai_base_url" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN openai_base_url TEXT NOT NULL DEFAULT ''")
    if "openai_model" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN openai_model TEXT NOT NULL DEFAULT ''")
    if "proxy_url" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN proxy_url TEXT NOT NULL DEFAULT ''")
    if "proxy_bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN proxy_bootstrap_enabled INTEGER NOT NULL DEFAULT 0")
    if "proxy_explore_enabled" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN proxy_explore_enabled INTEGER NOT NULL DEFAULT 0")
    if "domain_blacklist_json" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN domain_blacklist_json TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE settings SET bootstrap_worker_count = 1 WHERE bootstrap_worker_count IS NULL OR bootstrap_worker_count < 1")
    conn.execute("UPDATE settings SET explore_worker_count = 2 WHERE explore_worker_count IS NULL OR explore_worker_count < 1")
    conn.execute("UPDATE settings SET reason_worker_count = 1 WHERE reason_worker_count IS NULL OR reason_worker_count < 1")
    conn.execute("UPDATE settings SET openai_base_url = '' WHERE openai_base_url IS NULL")
    conn.execute("UPDATE settings SET openai_model = '' WHERE openai_model IS NULL")
    conn.execute("UPDATE settings SET proxy_url = '' WHERE proxy_url IS NULL")
    conn.execute("UPDATE settings SET proxy_bootstrap_enabled = 0 WHERE proxy_bootstrap_enabled IS NULL")
    conn.execute("UPDATE settings SET proxy_explore_enabled = 0 WHERE proxy_explore_enabled IS NULL")
    conn.execute("UPDATE settings SET domain_blacklist_json = '[]' WHERE domain_blacklist_json IS NULL")


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "parent_project_id" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN parent_project_id TEXT")
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )
    _assign_default_project_group(conn)


def _ensure_project_group_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_groups (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            scope_json TEXT NOT NULL DEFAULT '{"targets":[],"exclude":[],"notes":""}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_groups)")}
    if "scope_json" not in columns:
        conn.execute("ALTER TABLE project_groups ADD COLUMN scope_json TEXT NOT NULL DEFAULT '{\"targets\":[],\"exclude\":[],\"notes\":\"\"}'")
    conn.execute("UPDATE project_groups SET scope_json = '{\"targets\":[],\"exclude\":[],\"notes\":\"\"}' WHERE scope_json IS NULL OR scope_json = ''")
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('project_group', 0)")
    _assign_default_project_group(conn)


def _ensure_pi_tools_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pi_tools (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            builtin INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'custom',
            schema_json TEXT NOT NULL DEFAULT '{}',
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pi_tools_enabled_name ON pi_tools(enabled, name)")


def _assign_default_project_group(conn: sqlite3.Connection) -> None:
    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "parent_project_id" not in project_columns:
        return
    has_tasks = conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None
    if not has_tasks:
        return
    default_id = "pg_001"
    conn.execute(
        """
        INSERT OR IGNORE INTO project_groups (id, title, description, status, created_at)
        VALUES (?, '默认项目', '自动归档已有任务的默认项目', 'active', strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (default_id,),
    )
    conn.execute("UPDATE counters SET value = MAX(value, 1) WHERE name = 'project_group'")
    conn.execute("UPDATE projects SET parent_project_id = ? WHERE parent_project_id IS NULL", (default_id,))


def _ensure_blackboard_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(blackboard_items)")}
    if not columns:
        return
    if "priority" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'")
    if "related_fact_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN related_fact_id TEXT")
    if "related_intent_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN related_intent_id TEXT")
    if "related_vuln_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN related_vuln_id TEXT")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
