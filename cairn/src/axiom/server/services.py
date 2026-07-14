from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
import fnmatch
import json
import logging
import os
from urllib.parse import urlparse

from fastapi import HTTPException

from axiom.server.models import BlackboardItem, Intent, ProjectMeta, ProjectReason, Vulnerability

LOG = logging.getLogger(__name__)

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0)")
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    assert row is not None
    return f"proj_{row['value']:03d}"


def next_project_group_id(conn: sqlite3.Connection) -> str:
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('project_group', 0)")
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project_group'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project_group'").fetchone()
    assert row is not None
    return f"pg_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_vuln_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "vuln", "v", project_id)


def next_blackboard_item_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "blackboard", "b", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def get_project_group_or_404(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM project_groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project group not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["concluded_at"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    import json as _json
    worker_pool: list[str] = []
    if "worker_pool" in row.keys() and row["worker_pool"]:
        try:
            parsed = _json.loads(row["worker_pool"])
            if isinstance(parsed, list):
                worker_pool = [str(item) for item in parsed if isinstance(item, str)]
        except Exception:
            pass
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        timeouts=_intent_timeouts_from_row(row),
        creator=row["creator"],
        worker=row["worker"],
        worker_pool=worker_pool,
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        archived=bool(row["archived"]) if "archived" in row.keys() else False,
    )


def _intent_timeouts_from_row(row: sqlite3.Row) -> dict[str, int] | None:
    keys = row.keys()
    if "execute_timeout_seconds" not in keys and "conclude_timeout_seconds" not in keys:
        return None
    execute_seconds = row["execute_timeout_seconds"] if "execute_timeout_seconds" in keys else None
    conclude_seconds = row["conclude_timeout_seconds"] if "conclude_timeout_seconds" in keys else None
    if execute_seconds is None and conclude_seconds is None:
        return None
    if execute_seconds is None or conclude_seconds is None:
        return None
    return {
        "execute_seconds": int(execute_seconds),
        "conclude_seconds": int(conclude_seconds),
    }


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def build_vulns(conn: sqlite3.Connection, project_id: str) -> list[Vulnerability]:
    rows = conn.execute(
        "SELECT * FROM vulns WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    vulns: list[Vulnerability] = []
    for row in rows:
        evidence = _parse_json_list(row["evidence_json"])
        steps = _parse_json_list(row["reproduction_steps_json"])
        keys = row.keys()
        vulns.append(
            Vulnerability(
                id=row["id"],
                title=row["title"] if row["title"] else "",
                type=row["type"] if "type" in keys else "",
                severity=row["severity"] if "severity" in keys else "info",
                description=row["description"],
                prerequisites=row["prerequisites"] if "prerequisites" in keys else "",
                impact=row["impact"],
                evidence=evidence,
                reproduction_steps=steps,
                recommendation=row["recommendation"] if "recommendation" in keys else "",
                affected_scope=row["affected_scope"] if "affected_scope" in keys else "",
                status=_normalize_vuln_status(row["status"]),
                status_reason=row["status_reason"] if "status_reason" in keys else "",
                source=row["source"],
                source_file=row["source_file"] if "source_file" in keys else "",
                reported_by=row["reported_by"],
                created_at=row["created_at"],
            )
        )
    return vulns


def _normalize_vuln_status(value: str | None) -> str:
    text = str(value or "").strip()
    mapping = {
        "pending": "待验证",
        "confirmed": "已验证",
        "ignored": "忽略",
        "rejected": "重复",
        "待审核": "待验证",
        "待确认": "待验证",
        "已确认": "已验证",
        "已忽略": "忽略",
        "已拒绝": "重复",
        "已上报外部": "待验证",
    }
    return mapping.get(text.lower(), mapping.get(text, text or "待验证"))


def build_blackboard_items_for_group(conn: sqlite3.Connection, group_id: str) -> list[BlackboardItem]:
    rows = conn.execute(
        "SELECT * FROM blackboard_items WHERE group_id = ? ORDER BY updated_at DESC, created_at DESC",
        (group_id,),
    ).fetchall()
    return [_blackboard_item_from_row(row) for row in rows]


def next_blackboard_item_id_for_group(conn: sqlite3.Connection, group_id: str) -> str:
    import uuid
    return f"b{uuid.uuid4().hex[:12]}"


def _blackboard_item_from_row(row: sqlite3.Row) -> BlackboardItem:
    return BlackboardItem(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        hint=row["hint"] if "hint" in row.keys() else "",
        category=row["category"],
        status=row["status"],
        priority=row["priority"] or "medium",
        related_fact_id=row["related_fact_id"],
        related_intent_id=row["related_intent_id"],
        related_vuln_id=row["related_vuln_id"],
        creator=row["creator"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def build_blackboard_items(conn: sqlite3.Connection, project_id: str) -> list[BlackboardItem]:
    rows = conn.execute(
        "SELECT * FROM blackboard_items WHERE project_id = ? ORDER BY updated_at DESC, created_at DESC",
        (project_id,),
    ).fetchall()
    return [_blackboard_item_from_row(row) for row in rows]


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    values: list[str] = []
    for item in payload:
        if isinstance(item, str):
            text = item.strip()
            if text:
                values.append(text)
    return values


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    worker_pool: list[str] = []
    if "worker_pool" in row.keys() and row["worker_pool"]:
        try:
            parsed = json.loads(row["worker_pool"])
            if isinstance(parsed, list):
                worker_pool = [str(item) for item in parsed if isinstance(item, str)]
        except json.JSONDecodeError:
            worker_pool = []
    return ProjectMeta(
        id=row["id"],
        parent_project_id=row["parent_project_id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        kk_report_enabled=bool(row["kk_report_enabled"]) if "kk_report_enabled" in row.keys() else True,
        depth_mode=bool(row["depth_mode"]) if "depth_mode" in row.keys() else False,
        guard_enabled=bool(row["guard_enabled"]) if "guard_enabled" in row.keys() else False,
        worker_pool=worker_pool,
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND concluded_at IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _extract_host(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if "://" in text:
        parsed = urlparse(text)
        return (parsed.hostname or "").strip().lower() or None
    host = text.split("/", 1)[0].split(":", 1)[0].strip().strip(".").lower()
    return host or None


def _unique_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in items:
        item = raw.strip().lower()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _match_scope(host: str, scope_rule: str) -> bool:
    pattern_host = _extract_host(scope_rule)
    if not pattern_host:
        return False
    if host == pattern_host:
        return True
    if fnmatch.fnmatch(host, pattern_host):
        return True
    return host.endswith(f".{pattern_host}")


def _resolve_targets_for_scope(
    *,
    targets: list[str] | None,
    require_targets: bool,
) -> list[str]:
    if not targets and require_targets:
        raise HTTPException(status_code=400, detail="intent.targets is required by server policy.")
    if not targets:
        return []
    normalized = _unique_list([_extract_host(item) or "" for item in targets])
    normalized = [item for item in normalized if item]
    if not normalized:
        raise HTTPException(status_code=400, detail="intent.targets must contain at least one valid host/ip.")
    return normalized


def validate_intent_scope(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    description: str,
    targets: list[str] | None,
) -> None:
    project_row = conn.execute("SELECT parent_project_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project_row or not project_row["parent_project_id"]:
        return
    group_row = conn.execute(
        "SELECT scope_json FROM project_groups WHERE id = ?",
        (project_row["parent_project_id"],),
    ).fetchone()
    if not group_row or not group_row["scope_json"]:
        return
    try:
        scope_data = json.loads(group_row["scope_json"])
    except Exception:
        return

    allow_list = _unique_list([_extract_host(item) or "" for item in scope_data.get("targets", [])])
    deny_list = _unique_list([_extract_host(item) or "" for item in scope_data.get("exclude", [])])
    allow_list = [item for item in allow_list if item]
    deny_list = [item for item in deny_list if item]

    require_targets = _env_truthy("AXIOM_REQUIRE_INTENT_TARGETS", default=True)
    # 仅当项目组配置了范围策略（白名单或黑名单非空）时才强制 intent 声明 targets；
    # 无任何范围策略时不需要 targets，否则会无谓地阻断正常的人工派发。
    require_targets = require_targets and bool(allow_list or deny_list)
    intent_targets = _resolve_targets_for_scope(targets=targets, require_targets=require_targets)
    if not intent_targets:
        return

    for host in intent_targets:
        for denied in deny_list:
            if _match_scope(host, denied):
                raise HTTPException(
                    status_code=400,
                    detail=f"Intent target '{host}' matched blacklist rule '{denied}'.",
                )

    if allow_list:
        for host in intent_targets:
            if any(_match_scope(host, allowed) for allowed in allow_list):
                return
        raise HTTPException(
            status_code=400,
            detail=f"Intent targets {intent_targets} do not match whitelist {allow_list}.",
        )
