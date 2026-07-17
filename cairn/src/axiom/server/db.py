from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

LOG_IMPORT = logging.getLogger(__name__)

_NEW_DB = Path.home() / ".local" / "share" / "axiom" / "axiom.db"
_OLD_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"


def _resolve_db_path() -> Path:
    """Return the active DB path, preferring new AXIOM location.

    If the new path already exists, use it.  If only the old Cairn path
    exists, keep using it so existing installations are not disrupted.
    A fresh install creates the new path.
    """
    if _NEW_DB.exists():
        return _NEW_DB
    if _OLD_DB.exists():
        return _OLD_DB
    return _NEW_DB


DEFAULT_DB: Path = _resolve_db_path()

_db_path: Path | None = None

DEFAULT_TASK_SCOPE_JSON = '{"targets":[],"exclude":[],"notes":""}'

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    bootstrap_worker_count INTEGER NOT NULL DEFAULT 1,
    explore_worker_count INTEGER NOT NULL DEFAULT 2,
    reason_worker_count INTEGER NOT NULL DEFAULT 1,
    crawler_active_window_seconds INTEGER NOT NULL DEFAULT 180,
    crawler_flush_window_seconds INTEGER NOT NULL DEFAULT 45,
    crawler_total_timeout_seconds INTEGER NOT NULL DEFAULT 1800,
    dispatch_runtime_interval INTEGER NOT NULL DEFAULT 3,
    dispatch_runtime_max_workers INTEGER NOT NULL DEFAULT 8,
    dispatch_runtime_max_running_projects INTEGER NOT NULL DEFAULT 3,
    dispatch_runtime_max_project_workers INTEGER NOT NULL DEFAULT 4,
    dispatch_runtime_healthcheck_timeout INTEGER NOT NULL DEFAULT 20,
    dispatch_runtime_worker_healthcheck TEXT NOT NULL DEFAULT 'startup_only',
    dispatch_runtime_prompt_group TEXT NOT NULL DEFAULT 'default',
    dispatch_bootstrap_timeout INTEGER NOT NULL DEFAULT 300,
    dispatch_bootstrap_conclude_timeout INTEGER NOT NULL DEFAULT 90,
    dispatch_explore_conclude_timeout INTEGER NOT NULL DEFAULT 90,
    dispatch_reason_max_intents INTEGER NOT NULL DEFAULT 2,
    dispatch_config_version INTEGER NOT NULL DEFAULT 1,
    dispatch_config_updated_at TEXT NOT NULL DEFAULT '',
    openai_base_url TEXT NOT NULL DEFAULT '',
    openai_model TEXT NOT NULL DEFAULT '',
    proxy_url TEXT NOT NULL DEFAULT '',
    proxy_bootstrap_enabled INTEGER NOT NULL DEFAULT 0,
    proxy_explore_enabled INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS project_groups (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    parent_project_id TEXT REFERENCES project_groups(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    kk_report_enabled INTEGER NOT NULL DEFAULT 1,
    depth_mode INTEGER NOT NULL DEFAULT 0,
    guard_enabled INTEGER NOT NULL DEFAULT 0,
    worker_pool TEXT NOT NULL DEFAULT '[]',
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
    execute_timeout_seconds INTEGER,
    conclude_timeout_seconds INTEGER,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    worker_pool TEXT NOT NULL DEFAULT '[]',
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


CREATE TABLE IF NOT EXISTS judge_settings (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    enabled_default INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'enforce',
    base_url TEXT NOT NULL DEFAULT 'https://api.siliconflow.cn/v1',
    model TEXT NOT NULL DEFAULT 'Qwen/Qwen3.5-9B',
    timeout_seconds INTEGER NOT NULL DEFAULT 3,
    whitelist_tools_json TEXT NOT NULL DEFAULT '["read","ls","grep","find"]',
    whitelist_bash_verbs_json TEXT NOT NULL DEFAULT '["pwd","whoami","id","uname","ls","cat","head","tail","grep","find","ifconfig","ip","env","printenv"]',
    system_prompt TEXT NOT NULL DEFAULT '',
    hard_block_rules_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT ''
);

INSERT OR IGNORE INTO judge_settings (singleton_id) VALUES (1);

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


CREATE TABLE IF NOT EXISTS tool_hook_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT '',
    intent_id TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '',
    worker TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    input_json TEXT NOT NULL DEFAULT '{}',
    normalized_json TEXT NOT NULL DEFAULT '{}',
    decision TEXT NOT NULL DEFAULT 'ALLOW',
    observed_decision TEXT NOT NULL DEFAULT 'ALLOW',
    decision_source TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'observe',
    model_name TEXT NOT NULL DEFAULT '',
    model_latency_ms INTEGER NOT NULL DEFAULT 0,
    raw_model_output TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_hook_events_project_created ON tool_hook_events(project_id, created_at);

CREATE TABLE IF NOT EXISTS dispatch_workers (
    name TEXT PRIMARY KEY,
    worker_type TEXT NOT NULL DEFAULT 'pi',
    task_types_json TEXT NOT NULL DEFAULT '[]',
    max_running INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    env_json TEXT NOT NULL DEFAULT '{}',
    disabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatcher_runtime_state (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    config_version INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT NOT NULL DEFAULT '',
    workers_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

INSERT OR IGNORE INTO dispatcher_runtime_state (singleton_id, config_version, applied_at, workers_json, metadata_json)
VALUES (1, 0, '', '[]', '{}');

CREATE TABLE IF NOT EXISTS vulns (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'info',
    description TEXT NOT NULL,
    prerequisites TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    reproduction_steps_json TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT NOT NULL DEFAULT '',
    affected_scope TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '待验证',
    status_reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'reason',
    source_file TEXT NOT NULL DEFAULT '',
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
    worker TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT '',
    logical_thread_id TEXT NOT NULL DEFAULT '',
    attempt_id TEXT NOT NULL DEFAULT '',
    intent_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    frontend_domain TEXT,
    frontend_url TEXT,
    backend_domain TEXT,
    api_path TEXT NOT NULL,
    api_method TEXT NOT NULL,
    api_purpose TEXT,
    auth_required TEXT NOT NULL DEFAULT 'unknown',
    auth_type TEXT,
    test_status TEXT NOT NULL DEFAULT 'untested',
    tested_at TEXT,
    known_vulns TEXT,
    source_file TEXT,
    sample_body TEXT,
    content_type TEXT,
    login_config_json TEXT,
    discovered_at TEXT NOT NULL DEFAULT '',
    UNIQUE(project_id, backend_domain, api_path, api_method)
);

CREATE INDEX IF NOT EXISTS idx_assets_project_id ON assets(project_id);
CREATE INDEX IF NOT EXISTS idx_assets_group_id ON assets(group_id);
CREATE INDEX IF NOT EXISTS idx_assets_test_status ON assets(test_status);

CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    auth_type TEXT NOT NULL DEFAULT 'cookie',
    credentials_json TEXT NOT NULL DEFAULT '{}',
    domains_json TEXT NOT NULL DEFAULT '[]',
    source_task_id TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    asset_id INTEGER,
    UNIQUE(group_id, label)
);

CREATE INDEX IF NOT EXISTS idx_credentials_group_id ON credentials(group_id);
"""


def _ensure_mcp_servers_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            command TEXT NOT NULL DEFAULT '',
            args_json TEXT NOT NULL DEFAULT '[]',
            env_json TEXT NOT NULL DEFAULT '{}',
            disabled INTEGER NOT NULL DEFAULT 0,
            url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_servers_disabled ON mcp_servers(disabled)")
    # Migration: add url column to existing DBs (no-op if already present)
    cur = conn.execute("PRAGMA table_info(mcp_servers)")
    cols = {row[1] for row in cur.fetchall()}
    if "url" not in cols:
        conn.execute("ALTER TABLE mcp_servers ADD COLUMN url TEXT")
        # Backfill: any prior url-only entry stored {"url": "..."} in args_json -> migrate to url column
        rows = conn.execute(
            "SELECT id, args_json FROM mcp_servers WHERE url IS NULL AND (command = '' OR command IS NULL)"
        ).fetchall()
        for row in rows:
            try:
                import json as _json
                parsed = _json.loads(row["args_json"])
                if isinstance(parsed, dict) and parsed.get("url"):
                    conn.execute(
                        "UPDATE mcp_servers SET url = ?, args_json = '[]' WHERE id = ?",
                        (parsed["url"], row["id"]),
                    )
            except Exception:
                pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_server_tools (
            id TEXT PRIMARY KEY,
            server_id TEXT NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
            server_name TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(server_id, name)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_server_tools_server_id ON mcp_server_tools(server_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_server_tools_enabled ON mcp_server_tools(enabled)")
    tool_cols = {row[1] for row in conn.execute("PRAGMA table_info(mcp_server_tools)").fetchall()}
    if "server_name" not in tool_cols:
        conn.execute("ALTER TABLE mcp_server_tools ADD COLUMN server_name TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE mcp_server_tools
            SET server_name = COALESCE((SELECT name FROM mcp_servers WHERE mcp_servers.id = mcp_server_tools.server_id), '')
            WHERE server_name = ''
            """
        )
    if "description" not in tool_cols:
        conn.execute("ALTER TABLE mcp_server_tools ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "source" not in tool_cols:
        conn.execute("ALTER TABLE mcp_server_tools ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")


def _ensure_tool_hook_events_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_hook_events (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT '',
            intent_id TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            worker TEXT NOT NULL DEFAULT '',
            tool_name TEXT NOT NULL DEFAULT '',
            tool_call_id TEXT NOT NULL DEFAULT '',
            input_json TEXT NOT NULL DEFAULT '{}',
            normalized_json TEXT NOT NULL DEFAULT '{}',
            decision TEXT NOT NULL DEFAULT 'ALLOW',
            observed_decision TEXT NOT NULL DEFAULT 'ALLOW',
            decision_source TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'observe',
            model_name TEXT NOT NULL DEFAULT '',
            model_latency_ms INTEGER NOT NULL DEFAULT 0,
            raw_model_output TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_hook_events_project_created ON tool_hook_events(project_id, created_at)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tool_hook_events)")}
    migrations = {
        "observed_decision": "ALTER TABLE tool_hook_events ADD COLUMN observed_decision TEXT NOT NULL DEFAULT 'ALLOW'",
        "mode": "ALTER TABLE tool_hook_events ADD COLUMN mode TEXT NOT NULL DEFAULT 'observe'",
        "raw_model_output": "ALTER TABLE tool_hook_events ADD COLUMN raw_model_output TEXT NOT NULL DEFAULT ''",
        "cwd": "ALTER TABLE tool_hook_events ADD COLUMN cwd TEXT NOT NULL DEFAULT ''",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)


def _ensure_judge_settings_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS judge_settings (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            enabled_default INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'enforce',
            base_url TEXT NOT NULL DEFAULT 'https://api.siliconflow.cn/v1',
            model TEXT NOT NULL DEFAULT 'Qwen/Qwen3.5-9B',
            timeout_seconds INTEGER NOT NULL DEFAULT 3,
            whitelist_tools_json TEXT NOT NULL DEFAULT '["read","ls","grep","find"]',
            whitelist_bash_verbs_json TEXT NOT NULL DEFAULT '["pwd","whoami","id","uname","ls","cat","head","tail","grep","find","ifconfig","ip","env","printenv"]',
            system_prompt TEXT NOT NULL DEFAULT '',
            hard_block_rules_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(judge_settings)")}
    migrations = {
        "enabled_default": "ALTER TABLE judge_settings ADD COLUMN enabled_default INTEGER NOT NULL DEFAULT 0",
        "mode": "ALTER TABLE judge_settings ADD COLUMN mode TEXT NOT NULL DEFAULT 'enforce'",
        "base_url": "ALTER TABLE judge_settings ADD COLUMN base_url TEXT NOT NULL DEFAULT 'https://api.siliconflow.cn/v1'",
        "model": "ALTER TABLE judge_settings ADD COLUMN model TEXT NOT NULL DEFAULT 'Qwen/Qwen3.5-9B'",
        "timeout_seconds": "ALTER TABLE judge_settings ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 3",
        "whitelist_tools_json": "ALTER TABLE judge_settings ADD COLUMN whitelist_tools_json TEXT NOT NULL DEFAULT '[\"read\",\"ls\",\"grep\",\"find\"]'",
        "whitelist_bash_verbs_json": "ALTER TABLE judge_settings ADD COLUMN whitelist_bash_verbs_json TEXT NOT NULL DEFAULT '[\"pwd\",\"whoami\",\"id\",\"uname\",\"ls\",\"cat\",\"head\",\"tail\",\"grep\",\"find\",\"ifconfig\",\"ip\",\"env\",\"printenv\"]'",
        "system_prompt": "ALTER TABLE judge_settings ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''",
        "hard_block_rules_json": "ALTER TABLE judge_settings ADD COLUMN hard_block_rules_json TEXT NOT NULL DEFAULT '[]'",
        "updated_at": "ALTER TABLE judge_settings ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.execute("INSERT OR IGNORE INTO judge_settings (singleton_id) VALUES (1)")
    conn.execute("UPDATE judge_settings SET mode = 'enforce' WHERE singleton_id = 1 AND mode NOT IN ('off','observe','guarded','enforce')")
    conn.execute("UPDATE judge_settings SET timeout_seconds = 3 WHERE singleton_id = 1 AND (timeout_seconds IS NULL OR timeout_seconds < 1)")
    from axiom.server.judge_settings import DEFAULT_JUDGE_HARD_BLOCK_RULES, DEFAULT_JUDGE_SYSTEM_PROMPT
    conn.execute(
        "UPDATE judge_settings SET system_prompt = ? WHERE singleton_id = 1 AND TRIM(COALESCE(system_prompt, '')) = ''",
        (DEFAULT_JUDGE_SYSTEM_PROMPT,),
    )
    import json
    conn.execute(
        "UPDATE judge_settings SET hard_block_rules_json = ? WHERE singleton_id = 1 AND TRIM(COALESCE(hard_block_rules_json, '')) IN ('', '[]')",
        (json.dumps(DEFAULT_JUDGE_HARD_BLOCK_RULES, ensure_ascii=False, separators=(",", ":")),),
    )


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
        _ensure_fact_columns(conn)
        _ensure_intent_columns(conn)
        _ensure_blackboard_columns(conn)
        _ensure_vuln_columns(conn)
        _ensure_pi_tools_schema(conn)
        _ensure_plan_settings_columns(conn)
        _ensure_crawler_schema(conn)
        _ensure_dispatch_workers_columns(conn)
        _ensure_assets_schema(conn)
        _ensure_credentials_schema(conn)
        _ensure_tool_executions_columns(conn)
        _ensure_mcp_servers_schema(conn)
        _ensure_tool_hook_events_schema(conn)
        _ensure_judge_settings_schema(conn)
        _migrate_assets_from_tmp(conn)
        _fix_asset_group_ids(conn)
        _migrate_task_scopes_from_groups(conn)
        _clear_project_group_scopes(conn)


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
    if "dispatch_runtime_interval" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_interval INTEGER NOT NULL DEFAULT 3")
    if "crawler_active_window_seconds" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN crawler_active_window_seconds INTEGER NOT NULL DEFAULT 180")
    if "crawler_flush_window_seconds" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN crawler_flush_window_seconds INTEGER NOT NULL DEFAULT 45")
    if "crawler_total_timeout_seconds" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN crawler_total_timeout_seconds INTEGER NOT NULL DEFAULT 1800")
    if "dispatch_runtime_max_workers" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_max_workers INTEGER NOT NULL DEFAULT 8")
    if "dispatch_runtime_max_running_projects" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_max_running_projects INTEGER NOT NULL DEFAULT 3")
    if "dispatch_runtime_max_project_workers" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_max_project_workers INTEGER NOT NULL DEFAULT 4")
    if "dispatch_runtime_healthcheck_timeout" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_healthcheck_timeout INTEGER NOT NULL DEFAULT 20")
    if "dispatch_runtime_worker_healthcheck" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_worker_healthcheck TEXT NOT NULL DEFAULT 'startup_only'")
    if "dispatch_runtime_prompt_group" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_runtime_prompt_group TEXT NOT NULL DEFAULT 'default'")
    if "dispatch_bootstrap_timeout" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_bootstrap_timeout INTEGER NOT NULL DEFAULT 300")
    if "dispatch_bootstrap_conclude_timeout" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_bootstrap_conclude_timeout INTEGER NOT NULL DEFAULT 90")
    if "dispatch_explore_conclude_timeout" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_explore_conclude_timeout INTEGER NOT NULL DEFAULT 90")
    if "dispatch_reason_max_intents" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_reason_max_intents INTEGER NOT NULL DEFAULT 2")
    if "dispatch_config_version" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_config_version INTEGER NOT NULL DEFAULT 1")
    if "dispatch_config_updated_at" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN dispatch_config_updated_at TEXT NOT NULL DEFAULT ''")
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
    if "proxy_enabled" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN proxy_enabled INTEGER NOT NULL DEFAULT 0")
        # Derive from old per-phase flags if they exist.
        if "proxy_bootstrap_enabled" in columns and "proxy_explore_enabled" in columns:
            conn.execute(
                "UPDATE settings SET proxy_enabled = 1 "
                "WHERE proxy_bootstrap_enabled = 1 OR proxy_explore_enabled = 1"
            )
    conn.execute("UPDATE settings SET bootstrap_worker_count = 1 WHERE bootstrap_worker_count IS NULL OR bootstrap_worker_count < 1")
    conn.execute("UPDATE settings SET explore_worker_count = 2 WHERE explore_worker_count IS NULL OR explore_worker_count < 1")
    conn.execute("UPDATE settings SET reason_worker_count = 1 WHERE reason_worker_count IS NULL OR reason_worker_count < 1")
    conn.execute("UPDATE settings SET crawler_active_window_seconds = 180 WHERE crawler_active_window_seconds IS NULL OR crawler_active_window_seconds < 15")
    conn.execute("UPDATE settings SET crawler_flush_window_seconds = 45 WHERE crawler_flush_window_seconds IS NULL OR crawler_flush_window_seconds < 5")
    conn.execute("UPDATE settings SET crawler_total_timeout_seconds = 1800 WHERE crawler_total_timeout_seconds IS NULL OR crawler_total_timeout_seconds < 60")
    conn.execute("UPDATE settings SET dispatch_runtime_interval = 3 WHERE dispatch_runtime_interval IS NULL OR dispatch_runtime_interval < 1")
    conn.execute("UPDATE settings SET dispatch_runtime_max_workers = 8 WHERE dispatch_runtime_max_workers IS NULL OR dispatch_runtime_max_workers < 1")
    conn.execute("UPDATE settings SET dispatch_runtime_max_running_projects = 3 WHERE dispatch_runtime_max_running_projects IS NULL OR dispatch_runtime_max_running_projects < 1")
    conn.execute("UPDATE settings SET dispatch_runtime_max_project_workers = 4 WHERE dispatch_runtime_max_project_workers IS NULL OR dispatch_runtime_max_project_workers < 1")
    conn.execute("UPDATE settings SET dispatch_runtime_healthcheck_timeout = 20 WHERE dispatch_runtime_healthcheck_timeout IS NULL OR dispatch_runtime_healthcheck_timeout < 1")
    conn.execute("UPDATE settings SET dispatch_runtime_worker_healthcheck = 'startup_only' WHERE dispatch_runtime_worker_healthcheck IS NULL OR dispatch_runtime_worker_healthcheck = ''")
    conn.execute("UPDATE settings SET dispatch_runtime_prompt_group = 'default' WHERE dispatch_runtime_prompt_group IS NULL OR dispatch_runtime_prompt_group = ''")
    conn.execute("UPDATE settings SET dispatch_bootstrap_timeout = 300 WHERE dispatch_bootstrap_timeout IS NULL OR dispatch_bootstrap_timeout < 1")
    conn.execute("UPDATE settings SET dispatch_bootstrap_conclude_timeout = 90 WHERE dispatch_bootstrap_conclude_timeout IS NULL OR dispatch_bootstrap_conclude_timeout < 1")
    conn.execute("UPDATE settings SET dispatch_explore_conclude_timeout = 90 WHERE dispatch_explore_conclude_timeout IS NULL OR dispatch_explore_conclude_timeout < 1")
    conn.execute("UPDATE settings SET dispatch_reason_max_intents = 2 WHERE dispatch_reason_max_intents IS NULL OR dispatch_reason_max_intents < 1")
    conn.execute("UPDATE settings SET dispatch_config_version = 1 WHERE dispatch_config_version IS NULL OR dispatch_config_version < 1")
    conn.execute("UPDATE settings SET dispatch_config_updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE dispatch_config_updated_at IS NULL OR dispatch_config_updated_at = ''")
    conn.execute("UPDATE settings SET openai_base_url = '' WHERE openai_base_url IS NULL")
    conn.execute("UPDATE settings SET openai_model = '' WHERE openai_model IS NULL")
    conn.execute("UPDATE settings SET proxy_url = '' WHERE proxy_url IS NULL")
    conn.execute("UPDATE settings SET proxy_bootstrap_enabled = 0 WHERE proxy_bootstrap_enabled IS NULL")
    conn.execute("UPDATE settings SET proxy_explore_enabled = 0 WHERE proxy_explore_enabled IS NULL")
    conn.execute("UPDATE settings SET proxy_enabled = 0 WHERE proxy_enabled IS NULL")


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
    if "kk_report_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN kk_report_enabled INTEGER NOT NULL DEFAULT 1")
    conn.execute("UPDATE projects SET kk_report_enabled = 1 WHERE kk_report_enabled IS NULL")
    if "depth_mode" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN depth_mode INTEGER NOT NULL DEFAULT 0")
    if "guard_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN guard_enabled INTEGER NOT NULL DEFAULT 0")
    if "worker_pool" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN worker_pool TEXT NOT NULL DEFAULT '[]'")
    if "task_scope_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN task_scope_enabled INTEGER NOT NULL DEFAULT 0")
    if "task_scope_json" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN task_scope_json TEXT NOT NULL DEFAULT '" + DEFAULT_TASK_SCOPE_JSON + "'")
    if "credential_headers" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN credential_headers TEXT NOT NULL DEFAULT ''")
    _assign_default_project_group(conn)


def _ensure_fact_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
    if "archived" not in columns:
        conn.execute("ALTER TABLE facts ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def _ensure_intent_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "worker_pool" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN worker_pool TEXT NOT NULL DEFAULT '[]'")
    if "archived" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "execute_timeout_seconds" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN execute_timeout_seconds INTEGER")
    if "conclude_timeout_seconds" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN conclude_timeout_seconds INTEGER")


def _ensure_plan_settings_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "plan_timeout" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN plan_timeout INTEGER NOT NULL DEFAULT 600")
    if "plan_worker_count" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN plan_worker_count INTEGER NOT NULL DEFAULT 1")
    conn.execute("UPDATE settings SET plan_timeout = 600 WHERE plan_timeout IS NULL OR plan_timeout < 1")
    conn.execute("UPDATE settings SET plan_worker_count = 1 WHERE plan_worker_count IS NULL OR plan_worker_count < 1")


def _ensure_dispatch_workers_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(dispatch_workers)")}
    if "disabled" not in columns:
        conn.execute("ALTER TABLE dispatch_workers ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")


def _ensure_tool_executions_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tool_executions)")}
    additions = {
        "worker": "TEXT NOT NULL DEFAULT ''",
        "task_type": "TEXT NOT NULL DEFAULT ''",
        "phase": "TEXT NOT NULL DEFAULT ''",
        "logical_thread_id": "TEXT NOT NULL DEFAULT ''",
        "attempt_id": "TEXT NOT NULL DEFAULT ''",
        "intent_id": "TEXT NOT NULL DEFAULT ''",
        "session_id": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE tool_executions ADD COLUMN {name} {ddl}")


def _ensure_project_group_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_groups (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
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
    if "group_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN group_id TEXT NOT NULL DEFAULT ''")
    if "priority" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium'")
    if "related_fact_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN related_fact_id TEXT")
    if "related_intent_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN related_intent_id TEXT")
    if "related_vuln_id" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN related_vuln_id TEXT")
    if "hint" not in columns:
        conn.execute("ALTER TABLE blackboard_items ADD COLUMN hint TEXT NOT NULL DEFAULT ''")


def _ensure_vuln_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(vulns)")}
    if not columns:
        return
    if "prerequisites" not in columns:
        conn.execute("ALTER TABLE vulns ADD COLUMN prerequisites TEXT NOT NULL DEFAULT ''")
    if "source_file" not in columns:
        conn.execute("ALTER TABLE vulns ADD COLUMN source_file TEXT NOT NULL DEFAULT ''")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()



def _ensure_assets_schema(conn: sqlite3.Connection) -> None:
    """Ensure assets table has all required columns (for upgrades)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(assets)")}
    if not columns:
        return  # Table created fresh by SCHEMA
    if "group_id" not in columns:
        conn.execute("ALTER TABLE assets ADD COLUMN group_id TEXT NOT NULL DEFAULT ''")
    if "discovered_at" not in columns:
        conn.execute("ALTER TABLE assets ADD COLUMN discovered_at TEXT NOT NULL DEFAULT ''")
    if "tested_at" not in columns:
        conn.execute("ALTER TABLE assets ADD COLUMN tested_at TEXT")
    if "sample_body" not in columns:
        conn.execute("ALTER TABLE assets ADD COLUMN sample_body TEXT")
    if "content_type" not in columns:
        conn.execute("ALTER TABLE assets ADD COLUMN content_type TEXT")
    if "login_config_json" not in columns:
        conn.execute("ALTER TABLE assets ADD COLUMN login_config_json TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_group_id ON assets(group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_test_status ON assets(test_status)")



def _migrate_task_scopes_from_groups(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.parent_project_id, p.task_scope_enabled, p.task_scope_json, g.scope_json
            FROM projects p
            LEFT JOIN project_groups g ON g.id = p.parent_project_id
            WHERE (p.task_scope_json IS NULL OR p.task_scope_json = ? OR p.task_scope_json = '')
            """,
            (DEFAULT_TASK_SCOPE_JSON,),
        ).fetchall()
        for row in rows:
            raw = row["scope_json"] or DEFAULT_TASK_SCOPE_JSON
            enabled = 0
            try:
                import json as _json
                parsed = _json.loads(raw)
                if isinstance(parsed, dict) and (parsed.get("targets") or parsed.get("exclude")):
                    enabled = 1
            except Exception:
                raw = DEFAULT_TASK_SCOPE_JSON
            conn.execute(
                "UPDATE projects SET task_scope_json = ?, task_scope_enabled = ? WHERE id = ?",
                (raw, enabled, row["id"]),
            )
    except Exception:
        pass



def _clear_project_group_scopes(conn: sqlite3.Connection) -> None:
    """Project-level scope is deprecated; task_scope_json is the single source of truth."""
    try:
        conn.execute(
            "UPDATE project_groups SET scope_json = ? WHERE scope_json IS NULL OR scope_json != ?",
            (DEFAULT_TASK_SCOPE_JSON, DEFAULT_TASK_SCOPE_JSON),
        )
    except Exception:
        pass

def _ensure_credentials_schema(conn: sqlite3.Connection) -> None:
    """Ensure credentials table exists (for upgrades)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credentials (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            auth_type TEXT NOT NULL DEFAULT 'cookie',
            credentials_json TEXT NOT NULL DEFAULT '{}',
            domains_json TEXT NOT NULL DEFAULT '[]',
            source_task_id TEXT NOT NULL DEFAULT '',
            captured_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(group_id, label)
        )
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(credentials)")}
    if "domains_json" not in columns:
        conn.execute("ALTER TABLE credentials ADD COLUMN domains_json TEXT NOT NULL DEFAULT '[]'")
    conn.execute("UPDATE credentials SET domains_json = '[]' WHERE domains_json IS NULL")
    if "asset_id" not in columns:
        conn.execute("ALTER TABLE credentials ADD COLUMN asset_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_credentials_group_id ON credentials(group_id)")


def _migrate_assets_from_tmp(conn: sqlite3.Connection) -> None:
    """One-time migration: import rows from /tmp/axiom-assets.db if it exists."""
    tmp_db = Path("/tmp/axiom-assets.db")
    if not tmp_db.exists():
        return
    # Check if we already migrated (main DB has more than a handful of rows)
    row = conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()
    if row and row["n"] > 10:
        return
    try:
        src = sqlite3.connect(str(tmp_db))
        src.row_factory = sqlite3.Row
        # The tmp DB might exist as a stub (e.g. an empty 0-byte file) without an
        # assets table. Skip gracefully instead of logging a misleading exception.
        tables = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "assets" not in tables:
            src.close()
            return
        rows = src.execute("SELECT * FROM assets").fetchall()
        # Build project_id → parent_project_id lookup from main DB
        parent_map: dict[str, str] = {}
        try:
            for pr in conn.execute("SELECT id, parent_project_id FROM projects WHERE parent_project_id IS NOT NULL AND parent_project_id != ''").fetchall():
                parent_map[pr["id"]] = pr["parent_project_id"]
        except Exception:
            pass
        # Columns to skip: params_json exists in old DB but not in new
        skip_cols = {"params_json"}
        migrated = 0
        for r in rows:
            cols = [c for c in r.keys() if c not in skip_cols]
            values = [r[c] for c in cols]
            # Add group_id = parent_project_id (falls back to project_id)
            pid_idx = r.keys().index("project_id")
            pid = r[pid_idx]
            group_id = parent_map.get(pid, pid)
            cols.append("group_id")
            values.append(group_id)
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO assets ({col_names}) VALUES ({placeholders})",
                    values,
                )
                migrated += 1
            except Exception:
                pass
        conn.commit()
        src.close()
        LOG_IMPORT.info("migrated %d assets from %s", migrated, tmp_db)
    except Exception:
        LOG_IMPORT.exception("failed to migrate assets from %s", tmp_db)


def _fix_asset_group_ids(conn: sqlite3.Connection) -> None:
    """One-time fix: update group_id from parent_project_id for migrated assets."""
    try:
        rows = conn.execute(
            "SELECT id, parent_project_id FROM projects WHERE parent_project_id IS NOT NULL AND parent_project_id != ''"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE assets SET group_id = ? WHERE group_id = ? AND group_id != ?",
                (r["parent_project_id"], r["id"], r["parent_project_id"]),
            )
    except Exception:
        pass


def _ensure_crawler_schema(conn: sqlite3.Connection) -> None:
    """Create the crawler_workers table for standalone crawler configuration."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawler_workers (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            pi_model         TEXT NOT NULL DEFAULT '',
            pi_base_url      TEXT NOT NULL DEFAULT '',
            pi_api_key       TEXT NOT NULL DEFAULT '',
            pi_provider_api  TEXT NOT NULL DEFAULT 'openai-completions',
            pi_context_window TEXT NOT NULL DEFAULT '128000',
            updated_at       TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO crawler_workers (id, updated_at) VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawler_pages (
            group_id TEXT NOT NULL,
            page_id TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (group_id, page_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawler_apis (
            group_id TEXT NOT NULL,
            api_id TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (group_id, api_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawler_page_maps (
            group_id TEXT NOT NULL,
            map_id TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (group_id, map_id)
        )
        """
    )

    # Crawler tasks — standalone, not tied to project groups
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawler_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            target_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )

    # Migration: create crawler_tasks entries for existing group_id data
    _migrate_crawler_tasks(conn)


def _migrate_crawler_tasks(conn: sqlite3.Connection) -> None:
    """Create crawler_tasks entries for existing crawler data keyed by group_id."""
    import json as _json
    import os as _os

    # Check if migration was already done
    row = conn.execute("SELECT COUNT(*) AS n FROM crawler_tasks").fetchone()
    if row and row["n"] > 0:
        return

    # Find all distinct group_ids that have crawler data
    group_ids: set[str] = set()
    for table in ("crawler_pages", "crawler_apis", "crawler_page_maps"):
        try:
            rows = conn.execute(f"SELECT DISTINCT group_id FROM {table}").fetchall()
            group_ids.update(r["group_id"] for r in rows)
        except Exception:
            pass

    # Also check meta files on disk
    import glob as _glob
    for meta_path in _glob.glob("/tmp/axiom-crawler-*.meta.json"):
        try:
            with open(meta_path) as f:
                meta = _json.load(f)
            pid = meta.get("project_id") or ""
            if pid:
                group_ids.add(pid)
        except Exception:
            pass

    now = "2026-06-22T00:00:00Z"
    for gid in sorted(group_ids):
        # Try to get title from project_groups
        title = ""
        target_url = ""
        try:
            prow = conn.execute(
                "SELECT title FROM project_groups WHERE id = ?", (gid,)
            ).fetchone()
            if prow:
                title = prow["title"] or ""
        except Exception:
            pass

        # Try to get target_url from meta file
        meta_path = f"/tmp/axiom-crawler-{gid}.meta.json"
        if _os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = _json.load(f)
                target_url = meta.get("target_url", "")
            except Exception:
                pass

        conn.execute(
            "INSERT OR IGNORE INTO crawler_tasks (id, title, target_url, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (gid, title or gid, target_url, "idle", now, now),
        )
