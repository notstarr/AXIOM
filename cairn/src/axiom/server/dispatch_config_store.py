from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

import yaml

from axiom.server.db import current_db_path, get_conn


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DispatchConfigVersion:
    version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class DispatcherRuntimeState:
    config_version: int
    applied_at: str
    workers: list[dict[str, Any]]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchConfigSnapshot:
    version: DispatchConfigVersion
    config_data: dict[str, Any]
    runtime_state: DispatcherRuntimeState | None
    source: str


def load_dispatch_config_snapshot(fallback_path: Path) -> DispatchConfigSnapshot:
    fallback = read_dispatch_yaml(fallback_path)
    db_path = current_db_path()
    if not db_path.exists():
        return DispatchConfigSnapshot(
            version=DispatchConfigVersion(version=0, updated_at=""),
            config_data=_config_data_from_fallback(fallback),
            runtime_state=None,
            source="yaml",
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        settings = conn.execute(
            """
            SELECT intent_timeout, reason_timeout,
                bootstrap_worker_count, explore_worker_count, reason_worker_count,
                dispatch_runtime_interval, dispatch_runtime_max_workers,
                dispatch_runtime_max_running_projects, dispatch_runtime_max_project_workers,
                dispatch_runtime_healthcheck_timeout, dispatch_runtime_worker_healthcheck,
                dispatch_runtime_prompt_group, dispatch_bootstrap_timeout,
                dispatch_bootstrap_conclude_timeout, dispatch_explore_conclude_timeout,
                dispatch_reason_max_intents, dispatch_config_version, dispatch_config_updated_at,
                proxy_url, proxy_enabled
            FROM settings
            WHERE rowid = 1
            """
        ).fetchone()
        workers = conn.execute(
            """
            SELECT name, worker_type, task_types_json, max_running, priority, env_json, disabled
            FROM dispatch_workers
            ORDER BY name ASC
            """
        ).fetchall()
        state_row = conn.execute(
            """
            SELECT config_version, applied_at, workers_json, metadata_json
            FROM dispatcher_runtime_state
            WHERE singleton_id = 1
            """
        ).fetchone()
    except sqlite3.Error:
        return DispatchConfigSnapshot(
            version=DispatchConfigVersion(version=0, updated_at=""),
            config_data=_config_data_from_fallback(fallback),
            runtime_state=None,
            source="yaml",
        )
    finally:
        conn.close()

    if settings is None:
        return DispatchConfigSnapshot(
            version=DispatchConfigVersion(version=0, updated_at=""),
            config_data=_config_data_from_fallback(fallback),
            runtime_state=None,
            source="yaml",
        )

    worker_payload = [_worker_from_row(row) for row in workers]
    if not worker_payload:
        worker_payload = [_sanitize_worker_payload(worker) for worker in (fallback.get("workers") or [])]
    runtime_state = _runtime_state_from_row(state_row)
    return DispatchConfigSnapshot(
        version=DispatchConfigVersion(
            version=max(1, int(settings["dispatch_config_version"] or 1)),
            updated_at=str(settings["dispatch_config_updated_at"] or ""),
        ),
        config_data=_config_data_from_settings(settings, worker_payload, fallback),
        runtime_state=runtime_state,
        source="database" if workers else "database+yaml-workers",
    )


def read_dispatch_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}



DEFAULT_TASK_TYPES = ["bootstrap", "reason", "explore"]


def _sanitize_task_types(task_types: Any, *, default: list[str] | None = None) -> list[str]:
    allowed = {"bootstrap", "reason", "explore", "plan", "maintainer"}
    sanitized: list[str] = []
    for item in task_types or []:
        value = str(item)
        if value in allowed and value not in sanitized:
            sanitized.append(value)
    if sanitized:
        return sanitized
    if default is not None:
        return list(default)
    return list(DEFAULT_TASK_TYPES)

def write_dispatch_yaml(path: Path, body: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")


def peek_dispatch_config_version() -> DispatchConfigVersion:
    db_path = current_db_path()
    if not db_path.exists():
        return DispatchConfigVersion(version=0, updated_at="")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT dispatch_config_version, dispatch_config_updated_at FROM settings WHERE rowid = 1"
        ).fetchone()
    except sqlite3.Error:
        return DispatchConfigVersion(version=0, updated_at="")
    finally:
        conn.close()
    if row is None:
        return DispatchConfigVersion(version=0, updated_at="")
    return DispatchConfigVersion(
        version=max(1, int(row["dispatch_config_version"] or 1)),
        updated_at=str(row["dispatch_config_updated_at"] or ""),
    )


def write_dispatch_settings(body: dict[str, Any]) -> DispatchConfigVersion:
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT dispatch_config_version FROM settings WHERE rowid = 1"
        ).fetchone()
        version = max(1, int(row["dispatch_config_version"] or 1)) + 1 if row else 1
        conn.execute(
            """
            UPDATE settings
            SET intent_timeout = ?,
                reason_timeout = ?,
                bootstrap_worker_count = ?,
                explore_worker_count = ?,
                reason_worker_count = ?,
                dispatch_runtime_interval = ?,
                dispatch_runtime_max_workers = ?,
                dispatch_runtime_max_running_projects = ?,
                dispatch_runtime_max_project_workers = ?,
                dispatch_runtime_healthcheck_timeout = ?,
                dispatch_runtime_worker_healthcheck = ?,
                dispatch_runtime_prompt_group = ?,
                dispatch_bootstrap_timeout = ?,
                dispatch_bootstrap_conclude_timeout = ?,
                dispatch_explore_conclude_timeout = ?,
                dispatch_reason_max_intents = ?,
                proxy_url = ?,
                proxy_enabled = ?,
                dispatch_config_version = ?,
                dispatch_config_updated_at = ?
            WHERE rowid = 1
            """,
            (
                body.get("explore_timeout"),
                body.get("reason_timeout"),
                body.get("bootstrap_worker_count", 1),
                body.get("explore_worker_count", 2),
                body.get("reason_worker_count", 1),
                body.get("runtime_interval"),
                body.get("runtime_max_workers"),
                body.get("runtime_max_running_projects"),
                body.get("runtime_max_project_workers"),
                body.get("runtime_healthcheck_timeout"),
                body.get("runtime_worker_healthcheck"),
                body.get("runtime_prompt_group"),
                body.get("bootstrap_timeout"),
                body.get("bootstrap_conclude_timeout"),
                body.get("explore_conclude_timeout"),
                body.get("reason_max_intents"),
                body.get("proxy_url"),
                1 if body.get("proxy_enabled") else 0,
                version,
                now,
            ),
        )
        conn.execute("DELETE FROM dispatch_workers")
        if body["workers"]:
            conn.executemany(
                """
                INSERT INTO dispatch_workers (
                    name, worker_type, task_types_json, max_running, priority, env_json, disabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        worker["name"],
                        worker.get("type", "pi"),
                        json.dumps(_sanitize_task_types(worker.get("task_types")), ensure_ascii=False, separators=(",", ":")),
                        worker["max_running"],
                        worker["priority"],
                        json.dumps(worker["env"], ensure_ascii=False, separators=(",", ":")),
                        1 if worker.get("disabled") else 0,
                        now,
                    )
                    for worker in body["workers"]
                ],
            )
    return DispatchConfigVersion(version=version, updated_at=now)


def bump_dispatch_config_version() -> DispatchConfigVersion:
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT dispatch_config_version FROM settings WHERE rowid = 1"
        ).fetchone()
        version = max(1, int(row["dispatch_config_version"] or 1)) + 1 if row else 1
        conn.execute(
            """
            UPDATE settings
            SET dispatch_config_version = ?,
                dispatch_config_updated_at = ?
            WHERE rowid = 1
            """,
            (version, now),
        )
    return DispatchConfigVersion(version=version, updated_at=now)


def write_dispatcher_runtime_state(
    *,
    config_version: int,
    workers: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> None:
    db_path = current_db_path()
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO dispatcher_runtime_state (singleton_id, config_version, applied_at, workers_json, metadata_json)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                config_version = excluded.config_version,
                applied_at = excluded.applied_at,
                workers_json = excluded.workers_json,
                metadata_json = excluded.metadata_json
            """,
            (
                config_version,
                utcnow(),
                json.dumps(workers, ensure_ascii=False, separators=(",", ":")),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
    finally:
        conn.close()




def _sanitize_worker_payload(worker: Any) -> dict[str, Any]:
    if not isinstance(worker, dict):
        return {}
    payload = dict(worker)
    task_types = _sanitize_task_types(payload.get("task_types"), default=[])
    if task_types:
        payload["task_types"] = task_types
    else:
        # Keep invalid/empty legacy rows visible in settings without allowing
        # them to consume work until an operator selects at least one phase.
        payload["task_types"] = list(DEFAULT_TASK_TYPES)
        payload["disabled"] = True
    return payload

def _config_data_from_fallback(fallback: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(fallback.get("runtime") or {})
    tasks = dict(fallback.get("tasks") or {})
    return {
        "server": fallback.get("server", "http://127.0.0.1:8888"),
        "runtime": {
            "interval": runtime.get("interval", 3),
            "max_workers": runtime.get("max_workers", 8),
            "max_running_projects": runtime.get("max_running_projects", 3),
            "max_project_workers": runtime.get("max_project_workers", 4),
            "healthcheck_timeout": runtime.get("healthcheck_timeout", 20),
            "worker_healthcheck": runtime.get("worker_healthcheck", "startup_only"),
            "prompt_group": runtime.get("prompt_group", "default"),
        },
        "tasks": {
            "bootstrap": {
                "timeout": ((tasks.get("bootstrap") or {}).get("timeout", 300)),
                "conclude_timeout": ((tasks.get("bootstrap") or {}).get("conclude_timeout", 90)),
            },
            "explore": {
                "timeout": ((tasks.get("explore") or {}).get("timeout", 300)),
                "conclude_timeout": ((tasks.get("explore") or {}).get("conclude_timeout", 90)),
            },
            "reason": {
                "timeout": ((tasks.get("reason") or {}).get("timeout", 300)),
                "max_intents": ((tasks.get("reason") or {}).get("max_intents", 2)),
            },
        },
        "container": fallback.get("container") or {},
        "common_env": fallback.get("common_env") or {},
        "workers": [_sanitize_worker_payload(worker) for worker in (fallback.get("workers") or [])],
    }


def _config_data_from_settings(
    settings: sqlite3.Row,
    workers: list[dict[str, Any]],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    base = _config_data_from_fallback(fallback)
    base["runtime"] = {
        "interval": max(1, int(settings["dispatch_runtime_interval"] or base["runtime"]["interval"])),
        "max_workers": max(1, int(settings["dispatch_runtime_max_workers"] or base["runtime"]["max_workers"])),
        "max_running_projects": max(
            1,
            int(settings["dispatch_runtime_max_running_projects"] or base["runtime"]["max_running_projects"]),
        ),
        "max_project_workers": max(
            1,
            int(settings["dispatch_runtime_max_project_workers"] or base["runtime"]["max_project_workers"]),
        ),
        "healthcheck_timeout": max(
            1,
            int(settings["dispatch_runtime_healthcheck_timeout"] or base["runtime"]["healthcheck_timeout"]),
        ),
        "worker_healthcheck": str(
            settings["dispatch_runtime_worker_healthcheck"] or base["runtime"]["worker_healthcheck"]
        ),
        "prompt_group": str(settings["dispatch_runtime_prompt_group"] or base["runtime"]["prompt_group"]),
    }
    base["tasks"] = {
        "bootstrap": {
            "timeout": max(1, int(settings["dispatch_bootstrap_timeout"] or base["tasks"]["bootstrap"]["timeout"])),
            "conclude_timeout": max(
                1,
                int(
                    settings["dispatch_bootstrap_conclude_timeout"]
                    or base["tasks"]["bootstrap"]["conclude_timeout"]
                ),
            ),
        },
        "explore": {
            "timeout": max(1, int(settings["intent_timeout"] or base["tasks"]["explore"]["timeout"])),
            "conclude_timeout": max(
                1,
                int(
                    settings["dispatch_explore_conclude_timeout"]
                    or base["tasks"]["explore"]["conclude_timeout"]
                ),
            ),
        },
        "reason": {
            "timeout": max(1, int(settings["reason_timeout"] or base["tasks"]["reason"]["timeout"])),
            "max_intents": max(
                1,
                int(settings["dispatch_reason_max_intents"] or base["tasks"]["reason"]["max_intents"]),
            ),
        },
    }
    base["workers"] = [_sanitize_worker_payload(worker) for worker in workers]
    base["phase_limits"] = {
        "bootstrap": max(1, int(settings["bootstrap_worker_count"] or 1)),
        "explore": max(1, int(settings["explore_worker_count"] or 1)),
        "reason": max(1, int(settings["reason_worker_count"] or 1)),
    }
    return base


def _worker_from_row(row: sqlite3.Row) -> dict[str, Any]:
    env = _decode_json_dict(row["env_json"], {})
    # Keep the env as-is; no placeholder injection.  If PI_API_KEY is
    # missing or empty, pi will fall back to its own env or models.json.
    task_types = _sanitize_task_types(_decode_json_list(row["task_types_json"], []), default=[])
    has_no_task_types = not task_types
    return {
        "name": str(row["name"]),
        "type": str(row["worker_type"] or "pi"),
        "task_types": task_types or list(DEFAULT_TASK_TYPES),
        "max_running": max(1, int(row["max_running"] or 1)),
        "priority": max(0, int(row["priority"] or 0)),
        "env": {str(key): str(value) for key, value in env.items()},
        "disabled": bool(row["disabled"]) or has_no_task_types,
    }


def _runtime_state_from_row(row: sqlite3.Row | None) -> DispatcherRuntimeState | None:
    if row is None:
        return None
    workers = []
    for item in _decode_json_list(row["workers_json"], []):
        if not isinstance(item, dict):
            continue
        worker = dict(item)
        worker["task_types"] = _sanitize_task_types(worker.get("task_types"))
        workers.append(worker)
    return DispatcherRuntimeState(
        config_version=max(0, int(row["config_version"] or 0)),
        applied_at=str(row["applied_at"] or ""),
        workers=workers,
        metadata=_decode_json_dict(row["metadata_json"], {}),
    )


def _decode_json_dict(raw: Any, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def _decode_json_list(raw: Any, default: list[Any]) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return list(default)
    return value if isinstance(value, list) else list(default)
