from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from axiom.server.db import get_conn


router = APIRouter(tags=["diagnostics"])


BLOCKER_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "credential_expired",
        "凭证失效",
        "high",
        ("凭证过期", "凭证失效", "token过期", "token 过期", "jwt已失效", "jwt 已失效", "登录过期", "session expired", "unauthorized", "401"),
    ),
    (
        "request_template_incomplete",
        "请求模板不完整",
        "medium",
        ("请求头缺失", "请求体不全", "参数缺失", "header缺失", "headers缺失", "缺少请求", "missing header", "missing parameter"),
    ),
    (
        "rate_limited_or_risk_blocked",
        "限流或风控",
        "medium",
        ("活动太火爆", "太火爆", "访问频繁", "请求频繁", "限流", "风控", "429", "rate limit", "too many requests"),
    ),
    (
        "runtime_context_required",
        "需要运行态上下文",
        "medium",
        ("真实流量", "运行态", "storage", "openid", "unionid", "加密参数", "签名参数", "动态参数", "anti-content", "x-token"),
    ),
    (
        "manual_only",
        "需要人工介入",
        "low",
        ("人工", "手动", "扫码", "验证码", "人机", "短信码", "captcha", "manual"),
    ),
)

DIAGNOSTIC_TASK_TYPES: tuple[str, ...] = ("bootstrap", "explore", "reason", "maintainer")
TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)

FAILURE_REASON_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "timeout",
        "执行超时/被杀死",
        "high",
        ("timed_out", "timed out", "timeout", "超时", '"returncode": -9', "returncode=-9", "sigkill", "killed"),
    ),
    (
        "rate_limited",
        "限流/请求过频",
        "medium",
        ("429", "rate limit", "too many requests", "请求频繁", "访问频繁", "活动太火爆", "太火爆", "限流"),
    ),
    (
        "quota_exhausted",
        "模型额度不足",
        "high",
        ("quota", "insufficient_quota", "额度", "余额不足", "credit", "billing"),
    ),
    (
        "connection_error",
        "连接/API 调用错误",
        "medium",
        ("connection error", "connectionerror", "connect timeout", "econnreset", "502", "503", "504", "bad gateway"),
    ),
    (
        "parse_failure",
        "输出解析失败",
        "medium",
        ("parse", "invalid json", "jsondecode", "无法解析", "解析失败", "not valid json"),
    ),
    (
        "worker_unhealthy",
        "Worker 健康检查失败",
        "medium",
        ("unhealthy", "healthcheck", "health check", "心跳", "heartbeat lost", "worker offline"),
    ),
    (
        "credential_or_auth",
        "凭证/鉴权失败",
        "medium",
        ("401", "403", "unauthorized", "forbidden", "凭证", "鉴权", "认证", "token expired", "session expired"),
    ),
    (
        "tool_error",
        "工具执行失败",
        "low",
        ("tool error", "command failed", "returncode", "stderr", "exception", "traceback"),
    ),
)


def _json_loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_since(value: str | None) -> int | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _csv_set(raw: str) -> set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def _env_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:3]}***{text[-3:]}"


def _classify_text(text: str) -> list[dict[str, str]]:
    normalized = text.lower()
    matches: list[dict[str, str]] = []
    for code, label, severity, needles in BLOCKER_RULES:
        if any(needle.lower() in normalized for needle in needles):
            matches.append({"code": code, "label": label, "severity": severity})
    return matches


def _first_line(text: str, limit: int = 180) -> str:
    line = re.sub(r"\s+", " ", text or "").strip()
    return line[:limit]


def _event_text(row: Any, payload: Any | None = None) -> str:
    if payload is None:
        payload = _json_loads(row["payload_json"], {})
    try:
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except TypeError:
        payload_text = str(payload)
    return " ".join(
        str(item or "")
        for item in (row["task_type"], row["phase"], row["worker"], row["level"], row["message"], payload_text)
    )


def _classify_failure_reason(text: str, status: str) -> dict[str, str]:
    normalized = text.lower()
    for code, label, severity, needles in FAILURE_REASON_RULES:
        if any(needle.lower() in normalized for needle in needles):
            return {"code": code, "label": label, "severity": severity}
    if status == "timed_out":
        return {"code": "timeout", "label": "执行超时/被杀死", "severity": "high"}
    return {"code": "unknown_failed", "label": "未归类失败", "severity": "low"}


def _normalize_outcome(payload: dict[str, Any], context_text: str) -> str:
    outcome = str(payload.get("outcome") or payload.get("status") or "").strip().lower()
    normalized = context_text.lower()
    if outcome in {"success", "succeeded", "complete", "completed", "ok"}:
        return "success"
    if outcome in {"timed_out", "timeout", "hard_timeout", "hard_timeout_execute", "hard_timeout_conclude", "hard_timeout_both"}:
        return "timed_out"
    if any(marker in normalized for marker in ("timed_out", "timed out", "timeout", "超时", '"returncode": -9', "returncode=-9")):
        return "timed_out"
    if outcome in {"failed", "failure", "error", "parse_failure", "unhealthy", "needs_replan", "cancelled", "canceled"}:
        return "failed"
    if outcome:
        return "failed"
    return "other"


def _empty_worker_board(task_type: str) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "total": 0,
        "success": 0,
        "failed": 0,
        "timed_out": 0,
        "other": 0,
        "workers": [],
        "reasons": [],
    }


def _build_worker_rankings(events: list[Any]) -> dict[str, Any]:
    boards: dict[str, dict[str, Any]] = {task_type: _empty_worker_board(task_type) for task_type in DIAGNOSTIC_TASK_TYPES}
    by_task_worker: dict[tuple[str, str], list[Any]] = {}
    # Total runs per (task_type, worker) are counted from "phase finished" events, which are
    # emitted for EVERY completed worker run (success OR failure). This excludes "*_conclude"
    # synthesis steps so a single logical run is not counted twice. The previous implementation
    # only counted "task outcome" events, which are emitted on failure/timeout ONLY (loop.py
    # skips reporting when outcome == "success"), so the real success count was always invisible.
    run_totals: dict[tuple[str, str], int] = {}
    for row in events:
        task_type = row["task_type"] or ""
        if task_type not in boards:
            continue
        by_task_worker.setdefault((task_type, row["worker"] or ""), []).append(row)
        if row["message"] == "phase finished" and not (row["phase"] or "").endswith("_conclude"):
            key = (task_type, row["worker"] or "未标记 worker")
            run_totals[key] = run_totals.get(key, 0) + 1

    for row in events:
        task_type = row["task_type"] or ""
        if task_type not in boards or row["phase"] != "task_outcome":
            continue
        worker = row["worker"] or "未标记 worker"
        payload = _json_loads(row["payload_json"], {})
        if not isinstance(payload, dict):
            payload = {}

        context_rows = [row]
        for candidate in by_task_worker.get((task_type, worker), []):
            if candidate["id"] >= row["id"]:
                continue
            if candidate["phase"] == "task_outcome":
                break
            context_rows.append(candidate)
            if len(context_rows) >= 8:
                break
        context_text = " ".join(_event_text(item, payload if item["id"] == row["id"] else None) for item in context_rows)
        status = _normalize_outcome(payload, context_text)
        if status not in {"success", "failed", "timed_out"}:
            status = "other"

        workers_by_name = boards[task_type].setdefault("_workers_by_name", {})
        worker_item = workers_by_name.setdefault(worker, {
            "worker": worker,
            "total": 0,
            "success": 0,
            "failed": 0,
            "timed_out": 0,
            "other": 0,
            "latest_at": "",
            "reasons": {},
        })
        worker_item[status] += 1
        if not worker_item["latest_at"] or str(row["created_at"] or "") > worker_item["latest_at"]:
            worker_item["latest_at"] = row["created_at"] or ""

        if status in {"failed", "timed_out"}:
            reason = _classify_failure_reason(context_text, status)
            reason_bucket = worker_item["reasons"].setdefault(reason["code"], {
                **reason,
                "count": 0,
                "examples": [],
            })
            reason_bucket["count"] += 1
            example = _first_line(context_text, 220)
            if example and len(reason_bucket["examples"]) < 3:
                reason_bucket["examples"].append(example)

            phase_reasons = boards[task_type].setdefault("_reasons_by_code", {})
            phase_reason = phase_reasons.setdefault(reason["code"], {
                **reason,
                "count": 0,
                "workers": set(),
                "examples": [],
            })
            phase_reason["count"] += 1
            phase_reason["workers"].add(worker)
            if example and len(phase_reason["examples"]) < 3:
                phase_reason["examples"].append(example)

    for task_type, board in boards.items():
        workers_by_name = board.pop("_workers_by_name", {})
        # Seed worker items for runs that produced no "task outcome" event (e.g. an all-success
        # phase like bootstrap, or workers that only succeeded). Without this, successful runs
        # would be counted in run_totals but never surfaced on the board.
        for (tt, name), _total in run_totals.items():
            if tt == task_type and name not in workers_by_name:
                workers_by_name[name] = {
                    "worker": name,
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "timed_out": 0,
                    "other": 0,
                    "latest_at": "",
                    "reasons": {},
                }
        workers = []
        board_success = board_failed = board_timed_out = board_other = 0
        for name, worker_item in workers_by_name.items():
            total = run_totals.get((task_type, name), 0)
            failed = int(worker_item["failed"] or 0)
            timed_out = int(worker_item["timed_out"] or 0)
            other = int(worker_item["other"] or 0)
            success = max(0, total - failed - timed_out - other)
            worker_item["total"] = total
            worker_item["success"] = success
            worker_item["completion_rate"] = round(success / total, 4) if total else 0
            worker_item["failure_timeout_rate"] = round((failed + timed_out) / total, 4) if total else 0
            worker_item["rank_score"] = failed + timed_out
            worker_item["reasons"] = sorted(
                worker_item["reasons"].values(),
                key=lambda item: (-item["count"], item["label"]),
            )
            workers.append(worker_item)
            board_success += success
            board_failed += failed
            board_timed_out += timed_out
            board_other += other
        board["total"] = sum(w["total"] for w in workers)
        board["success"] = board_success
        board["failed"] = board_failed
        board["timed_out"] = board_timed_out
        board["other"] = board_other
        board["workers"] = sorted(
            workers,
            key=lambda item: (-item["completion_rate"], item["rank_score"], -item["total"], item["worker"]),
        )
        reasons = []
        for item in board.pop("_reasons_by_code", {}).values():
            item["workers"] = sorted(item["workers"])
            reasons.append(item)
        board["reasons"] = sorted(reasons, key=lambda item: (-item["count"], item["label"]))
    return {"phases": list(DIAGNOSTIC_TASK_TYPES), "boards": boards}


def _empty_token_bucket(name: str) -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "name": name,
        "calls": 0,
        "latest_at": "",
    }
    for field in TOKEN_FIELDS:
        bucket[field] = 0
    return bucket


def _normalize_token_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None

    def as_int(*keys: str) -> int:
        for key in keys:
            value = raw.get(key)
            if value is None:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            return max(0, parsed)
        return 0

    usage = {
        "input_tokens": as_int("input_tokens", "prompt_tokens", "inputTokens", "input"),
        "output_tokens": as_int("output_tokens", "completion_tokens", "outputTokens", "output"),
        "cache_read_tokens": as_int("cache_read_tokens", "cached_tokens", "cacheRead", "cache_read"),
        "cache_write_tokens": as_int("cache_write_tokens", "cacheWrite", "cache_write"),
        "reasoning_tokens": as_int("reasoning_tokens", "reasoningTokens", "reasoning"),
        "total_tokens": as_int("total_tokens", "totalTokens"),
    }
    if not any(usage.values()):
        return None
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = (
            usage["input_tokens"]
            + usage["output_tokens"]
            + usage["cache_read_tokens"]
            + usage["cache_write_tokens"]
        )
    return usage


def _add_token_usage(bucket: dict[str, Any], usage: dict[str, int], created_at: str | None) -> None:
    bucket["calls"] += 1
    for field in TOKEN_FIELDS:
        bucket[field] += int(usage.get(field, 0) or 0)
    if created_at and (not bucket["latest_at"] or str(created_at) > bucket["latest_at"]):
        bucket["latest_at"] = created_at


def _finalize_token_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        calls = int(row.get("calls") or 0)
        row["avg_total_tokens"] = round((int(row.get("total_tokens") or 0) / calls), 2) if calls else 0
        row["active_breakdown_tokens"] = (
            int(row.get("input_tokens") or 0)
            + int(row.get("output_tokens") or 0)
            + int(row.get("cache_read_tokens") or 0)
            + int(row.get("cache_write_tokens") or 0)
        )
    return sorted(rows, key=lambda item: (-int(item.get("total_tokens") or 0), -int(item.get("calls") or 0), item.get("name") or ""))


def _build_token_rankings(events: list[Any]) -> dict[str, Any]:
    candidates: list[tuple[Any, dict[str, Any], dict[str, int]]] = []
    task_types_with_session_usage: set[str] = set()
    for row in events:
        task_type = row["task_type"] or ""
        if task_type not in DIAGNOSTIC_TASK_TYPES:
            continue
        payload = _json_loads(row["payload_json"], {})
        if not isinstance(payload, dict):
            continue
        usage = _normalize_token_usage(payload.get("token_usage"))
        if usage is None:
            continue
        if row["message"] == "token usage":
            task_types_with_session_usage.add(task_type)
        candidates.append((row, payload, usage))

    by_worker: dict[str, dict[str, Any]] = {}
    by_phase: dict[str, dict[str, Any]] = {phase: _empty_token_bucket(phase) for phase in DIAGNOSTIC_TASK_TYPES}
    totals = _empty_token_bucket("total")
    included_events = 0
    ignored_possible_duplicates = 0

    for row, _payload, usage in candidates:
        task_type = row["task_type"] or ""
        if task_type in task_types_with_session_usage and row["message"] != "token usage":
            ignored_possible_duplicates += 1
            continue
        worker = row["worker"] or "未标记 worker"
        worker_bucket = by_worker.setdefault(worker, {**_empty_token_bucket(worker), "phases": set()})
        worker_bucket["phases"].add(task_type)
        phase_bucket = by_phase.setdefault(task_type, {**_empty_token_bucket(task_type), "workers": set()})
        phase_bucket.setdefault("workers", set()).add(worker)
        created_at = row["created_at"] or ""
        _add_token_usage(worker_bucket, usage, created_at)
        _add_token_usage(phase_bucket, usage, created_at)
        _add_token_usage(totals, usage, created_at)
        included_events += 1

    worker_rows = []
    for item in by_worker.values():
        item["worker"] = item["name"]
        item["phases"] = sorted(item.get("phases") or [])
        worker_rows.append(item)
    phase_rows = []
    for phase in DIAGNOSTIC_TASK_TYPES:
        item = by_phase.get(phase, _empty_token_bucket(phase))
        workers = sorted(item.get("workers") or [])
        item["phase"] = phase
        item["workers"] = workers
        item["worker_count"] = len(workers)
        phase_rows.append(item)

    return {
        "totals": totals,
        "by_worker": _finalize_token_rows(worker_rows),
        "by_phase": _finalize_token_rows(phase_rows),
        "included_events": included_events,
        "ignored_possible_duplicates": ignored_possible_duplicates,
        "notes": [
            "优先使用 session 级 token usage 事件；同阶段存在 session 事件时忽略 phase finished 内可能重复的 token_usage。",
            "total_tokens 按模型返回的 totalTokens 汇总，可能包含 cacheRead/cacheWrite；新任务会额外展示 cache/reasoning 明细。",
        ],
    }


def _load_mcp_catalog(conn) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    servers: dict[str, dict[str, Any]] = {}
    tools_by_server: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute("SELECT id, name, command, url, disabled FROM mcp_servers ORDER BY name ASC").fetchall():
        server = {
            "id": row["id"],
            "name": row["name"],
            "command": row["command"] or "",
            "url": row["url"] or "",
            "disabled": bool(row["disabled"]),
            "tool_count": 0,
            "enabled_tool_count": 0,
        }
        servers[server["name"]] = server
        tools_by_server[server["name"]] = []
    for row in conn.execute(
        """
        SELECT server_name, name, description, enabled
        FROM mcp_server_tools
        ORDER BY server_name ASC, name ASC
        """
    ).fetchall():
        server_name = row["server_name"] or ""
        tool = {
            "name": row["name"],
            "description": row["description"] or "",
            "enabled": bool(row["enabled"]),
        }
        tools_by_server.setdefault(server_name, []).append(tool)
        if server_name in servers:
            servers[server_name]["tool_count"] += 1
            if tool["enabled"]:
                servers[server_name]["enabled_tool_count"] += 1
    return servers, tools_by_server


def _worker_capabilities(conn) -> list[dict[str, Any]]:
    servers, tools_by_server = _load_mcp_catalog(conn)
    workers: list[dict[str, Any]] = []
    rows = conn.execute(
        """
        SELECT name, worker_type, task_types_json, max_running, priority, env_json, disabled
        FROM dispatch_workers
        ORDER BY name ASC
        """
    ).fetchall()
    for row in rows:
        env = _json_loads(row["env_json"], {})
        if not isinstance(env, dict):
            env = {}
        allowed_servers = _csv_set(str(env.get("PI_MCP_ALLOWED_SERVERS", "")))
        allowed_tools = _csv_set(str(env.get("PI_MCP_ALLOWED_TOOLS", "")))
        visible_servers: list[dict[str, Any]] = []
        for server_name, server in servers.items():
            if allowed_servers and server_name not in allowed_servers:
                continue
            tools = []
            for tool in tools_by_server.get(server_name, []):
                scoped = f"{server_name}:{tool['name']}"
                if allowed_tools and tool["name"] not in allowed_tools and scoped not in allowed_tools:
                    continue
                tools.append(tool)
            visible_servers.append({
                **server,
                "tools": tools[:50],
                "visible_tool_count": len(tools),
                "visible_enabled_tool_count": len([tool for tool in tools if tool["enabled"]]),
            })
        workers.append({
            "name": row["name"],
            "worker_type": row["worker_type"],
            "task_types": _json_loads(row["task_types_json"], []),
            "max_running": row["max_running"],
            "priority": row["priority"],
            "disabled": bool(row["disabled"]),
            "model": env.get("PI_MODEL", ""),
            "base_url": env.get("PI_BASE_URL", ""),
            "provider_api": env.get("PI_PROVIDER_API", ""),
            "context_window": env.get("PI_MODEL_CONTEXT_WINDOW", ""),
            "api_key_masked": _mask_secret(str(env.get("PI_API_KEY", ""))),
            "mcp_enabled": _env_bool(env.get("PI_MCP_ENABLED", "")),
            "mcp_profile": env.get("PI_MCP_PROFILE", ""),
            "mcp_allowed_servers": sorted(allowed_servers),
            "mcp_allowed_tools": sorted(allowed_tools),
            "mcp_servers": visible_servers,
        })
    return workers


@router.get("/projects/{project_id}/diagnostics")
def get_project_diagnostics(project_id: str):
    with get_conn() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "Project not found")
        settings = conn.execute(
            "SELECT dispatch_config_version, dispatch_config_updated_at FROM settings WHERE rowid = 1"
        ).fetchone()
        runtime = conn.execute(
            "SELECT config_version, applied_at, workers_json, metadata_json FROM dispatcher_runtime_state WHERE singleton_id = 1"
        ).fetchone()
        intents = conn.execute(
            """
            SELECT id, description, creator, worker, worker_pool, execute_timeout_seconds,
                   conclude_timeout_seconds, last_heartbeat_at, created_at, concluded_at
            FROM intents
            WHERE project_id = ?
            ORDER BY created_at ASC
            """,
            (project_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT id, task_type, phase, worker, level, message, payload_json, created_at
            FROM task_events
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 80
            """,
            (project_id,),
        ).fetchall()
        stats_events = conn.execute(
            """
            SELECT id, task_type, phase, worker, level, message, payload_json, created_at
            FROM task_events
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT 2000
            """,
            (project_id,),
        ).fetchall()
        facts = conn.execute(
            "SELECT id, description FROM facts WHERE project_id = ? ORDER BY id DESC LIMIT 80",
            (project_id,),
        ).fetchall()
        vulns = conn.execute(
            "SELECT id, title, description, status FROM vulns WHERE project_id = ? ORDER BY created_at DESC LIMIT 40",
            (project_id,),
        ).fetchall()
        worker_capabilities = _worker_capabilities(conn)

    active_intents = [row for row in intents if not row["concluded_at"]]
    unclaimed_intents = [row for row in active_intents if not row["worker"]]
    running_intents = [row for row in active_intents if row["worker"]]
    latest_event = events[0] if events else None
    latest_event_age = _seconds_since(latest_event["created_at"] if latest_event else None)
    worker_rankings = _build_worker_rankings(list(stats_events))
    token_rankings = _build_token_rankings(list(stats_events))

    stuck_intents = []
    for row in running_intents:
        timeout = int(row["execute_timeout_seconds"] or 0) or 300
        heartbeat_age = _seconds_since(row["last_heartbeat_at"] or row["created_at"])
        if heartbeat_age is not None and heartbeat_age > timeout:
            stuck_intents.append({
                "id": row["id"],
                "worker": row["worker"],
                "heartbeat_age_seconds": heartbeat_age,
                "timeout_seconds": timeout,
                "description": _first_line(row["description"]),
            })

    event_items = []
    for row in events[:20]:
        payload = _json_loads(row["payload_json"], {})
        event_items.append({
            "id": row["id"],
            "task_type": row["task_type"],
            "phase": row["phase"],
            "worker": row["worker"],
            "level": row["level"],
            "message": row["message"],
            "payload": payload if isinstance(payload, dict) else {},
            "created_at": row["created_at"],
        })

    phase_counts: dict[str, int] = {}
    for row in events:
        key = f"{row['task_type']}:{row['phase']}"
        phase_counts[key] = phase_counts.get(key, 0) + 1

    evidence_sources: list[tuple[str, str, str]] = []
    for row in facts:
        evidence_sources.append(("fact", row["id"], row["description"]))
    for row in events:
        evidence_sources.append(("event", str(row["id"]), f"{row['message']} {row['payload_json']}"))
    for row in vulns:
        evidence_sources.append(("vuln", row["id"], f"{row['title']} {row['description']}"))

    blockers_by_code: dict[str, dict[str, Any]] = {}
    for source_type, source_id, text in evidence_sources:
        for match in _classify_text(text):
            item = blockers_by_code.setdefault(match["code"], {
                **match,
                "count": 0,
                "evidence": [],
            })
            item["count"] += 1
            if len(item["evidence"]) < 5:
                item["evidence"].append({
                    "source_type": source_type,
                    "source_id": source_id,
                    "text": _first_line(text),
                })
    blockers = sorted(
        blockers_by_code.values(),
        key=lambda item: ({"high": 0, "medium": 1, "low": 2}.get(item["severity"], 3), -item["count"]),
    )

    configured_version = int(settings["dispatch_config_version"] or 0) if settings else 0
    applied_version = int(runtime["config_version"] or 0) if runtime else 0
    runtime_workers = _json_loads(runtime["workers_json"] if runtime else "[]", [])
    runtime_worker_names = [str(item.get("name")) for item in runtime_workers if isinstance(item, dict) and item.get("name")]

    health_status = "ok"
    health_notes: list[str] = []
    if applied_version < configured_version:
        health_status = "warning"
        health_notes.append("dispatcher 尚未应用最新配置")
    if unclaimed_intents and not runtime_worker_names:
        health_status = "warning"
        health_notes.append("存在未领取意图，但 dispatcher 未上报可用 worker")
    if stuck_intents:
        health_status = "danger"
        health_notes.append("存在疑似超时的执行中意图")
    if blockers:
        health_notes.append("检测到可能阻塞原因，可在阻塞分类中查看")

    return {
        "project": {
            "id": project["id"],
            "title": project["title"],
            "status": project["status"],
            "created_at": project["created_at"],
            "worker_pool": _json_loads(project["worker_pool"], []),
            "reason_worker": project["reason_worker"],
            "reason_trigger": project["reason_trigger"],
            "reason_last_heartbeat_age_seconds": _seconds_since(project["reason_last_heartbeat_at"]),
        },
        "health": {
            "status": health_status,
            "notes": health_notes,
            "latest_phase": f"{latest_event['task_type']}:{latest_event['phase']}" if latest_event else "",
            "latest_event_age_seconds": latest_event_age,
            "phase_counts": phase_counts,
            "active_intent_count": len(active_intents),
            "unclaimed_intent_count": len(unclaimed_intents),
            "running_intent_count": len(running_intents),
            "stuck_intents": stuck_intents,
        },
        "config_status": {
            "configured_version": configured_version,
            "configured_at": settings["dispatch_config_updated_at"] if settings else "",
            "applied_version": applied_version,
            "applied_at": runtime["applied_at"] if runtime else "",
            "is_current": applied_version >= configured_version and configured_version > 0,
            "runtime_worker_names": runtime_worker_names,
        },
        "workers": worker_capabilities,
        "worker_rankings": worker_rankings,
        "token_rankings": token_rankings,
        "blockers": blockers,
        "latest_events": event_items,
    }
