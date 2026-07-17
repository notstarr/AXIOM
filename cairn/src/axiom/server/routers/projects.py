import json
import os as _os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile

from axiom.server.db import get_conn
from axiom.server.models import (
    BlackboardItem,
    BlackboardBatchStatusUpdateRequest,
    CompleteRequest,
    CreateBlackboardItemRequest,
    CreateProjectGroupRequest,
    CreateProjectRequest,
    CreateVulnerabilityRequest,
    ExploreStartRequest,
    ExploreStatus,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectGroupSummary,
    ProjectMeta,
    ProjectSummary,
    TaskEvent,
    TaskEventCreateRequest,
    ReopenRequest,
    ReopenResponse,
    TaskScope,
    ReasonClaimRequest,
    UpdateProjectGroupRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    UpdateProjectWorkerPoolRequest,
    UpdateProjectWorkerPoolResponse,
    UpdateBlackboardItemRequest,
    Vulnerability,
)
from axiom.server.services import (
    build_blackboard_items,
    build_blackboard_items_for_group,
    validate_intent_scope,
    build_vulns,
    build_intents,
    check_project_completed,
    check_project_active,
    check_project_hint_writable,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    get_completion_intent_or_409,
    get_project_group_or_404,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    next_blackboard_item_id,
    next_blackboard_item_id_for_group,
    next_project_group_id,
    next_vuln_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["projects"])


def _scope_to_storage(scope: TaskScope | None) -> tuple[int, str]:
    if scope is None:
        return 0, json.dumps({"targets": [], "exclude": [], "notes": ""}, ensure_ascii=False, separators=(",", ":"))
    raw = json.dumps({"targets": scope.targets, "exclude": scope.exclude, "notes": scope.notes}, ensure_ascii=False, separators=(",", ":"))
    return (1 if scope.enabled else 0), raw


def _build_conversation_payload(
    conn,
    project_id: str,
    *,
    thread_id: str = "",
    thread_type: str = "",
    intent_id: str = "",
    worker: str = "",
    kind: str = "",
    query: str = "",
    limit: int = 800,
    include_items: bool = True,
) -> dict:
    event_rows = conn.execute(
        """
        SELECT id, project_id, task_type, phase, worker, level, message, payload_json, created_at
        FROM task_events
        WHERE project_id = ?
        ORDER BY id ASC
        LIMIT 5000
        """,
        (project_id,),
    ).fetchall()
    tool_rows = conn.execute(
        """
        SELECT id, tool, kind, argv_json, status, returncode, stdout, stderr, duration_ms,
               worker, task_type, phase, logical_thread_id, attempt_id, intent_id, session_id,
               created_at, finished_at
        FROM tool_executions
        WHERE project_id = ?
        ORDER BY created_at ASC
        LIMIT 3000
        """,
        (project_id,),
    ).fetchall()
    intent_rows = conn.execute(
        """
        SELECT id, to_fact_id, description, worker, created_at, concluded_at
        FROM intents
        WHERE project_id = ?
        ORDER BY id ASC
        """,
        (project_id,),
    ).fetchall()
    intents_by_id = {str(row["id"]): row for row in intent_rows}
    intent_windows = _conversation_intent_windows(intent_rows)

    threads: dict[str, dict] = {}
    attempts: dict[str, dict] = {}
    items: list[dict] = []
    active_attempts: dict[tuple[str, str, str], str] = {}
    reason_cycle = 1
    reason_cycle_closed = False
    bootstrap_intent_id = _project_bootstrap_intent_id(conn, project_id)

    def ensure_thread(tid: str, ttype: str, label: str, *, subtitle: str = "", intent: str = "") -> dict:
        thread = threads.get(tid)
        if thread is None:
            thread = {
                "id": tid,
                "type": ttype,
                "label": label,
                "subtitle": subtitle,
                "intent_id": intent,
                "status": "running",
                "attempt_count": 0,
                "timeout_count": 0,
                "error_count": 0,
                "workers": [],
                "attempts": [],
                "started_at": "",
                "last_at": "",
                "summary": "",
                "fact_id": "",
                "description": "",
                "worker_groups": [],
                "dispatched_intents": [],
                "related": {},
            }
            threads[tid] = thread
        return thread

    def ensure_intent_thread(intent_id_value: str) -> dict:
        row = intents_by_id.get(intent_id_value)
        thread = ensure_thread(
            f"intent:{project_id}:{intent_id_value}",
            "intent",
            _conversation_group_label("i", intent_id_value),
            subtitle=f"Intent 执行 · {intent_id_value}",
            intent=intent_id_value,
        )
        if row is not None:
            thread["fact_id"] = row["to_fact_id"] or ""
            thread["description"] = row["description"] or ""
            thread["related"] = {
                "intent_id": intent_id_value,
                "fact_id": row["to_fact_id"] or "",
                "description": row["description"] or "",
            }
            thread["business_status"] = "success" if row["concluded_at"] else ("running" if row["worker"] else "pending")
        return thread

    def ensure_attempt(aid: str, thread: dict, row: dict, payload: dict, role: str) -> dict:
        attempt = attempts.get(aid)
        if attempt is None:
            attempt = {
                "id": aid,
                "thread_id": thread["id"],
                "task_type": row["task_type"],
                "phase": row["phase"],
                "worker": row["worker"],
                "intent_id": payload.get("intent_id") or thread.get("intent_id") or "",
                "session_id": payload.get("session_id") or "",
                "role": role,
                "status": "running",
                "started_at": row["created_at"],
                "finished_at": "",
                "timeout_seconds": payload.get("timeout_seconds"),
                "duration_ms": None,
                "token_usage": {},
                "diagnostic": {},
                "item_count": 0,
            }
            attempts[aid] = attempt
            thread["attempts"].append(attempt)
            thread["attempt_count"] += 1
            if row["worker"] and row["worker"] not in thread["workers"]:
                thread["workers"].append(row["worker"])
            if not thread["started_at"] or row["created_at"] < thread["started_at"]:
                thread["started_at"] = row["created_at"]
        if payload.get("session_id") and not attempt.get("session_id"):
            attempt["session_id"] = payload.get("session_id")
        if payload.get("timeout_seconds") and not attempt.get("timeout_seconds"):
            attempt["timeout_seconds"] = payload.get("timeout_seconds")
        return attempt

    def append_item(item: dict) -> None:
        items.append(item)
        tid = item.get("thread_id") or ""
        aid = item.get("attempt_id") or ""
        if tid in threads:
            threads[tid]["last_at"] = max(threads[tid].get("last_at") or "", item.get("created_at") or "")
        if aid in attempts:
            attempts[aid]["item_count"] += 1

    for row in event_rows:
        payload = _json_dict(row["payload_json"])
        task_type = str(row["task_type"] or "system")
        phase = str(row["phase"] or task_type)
        msg = str(row["message"] or "")
        row_dict = dict(row)
        inferred_intent = str(payload.get("intent_id") or "")
        logical_thread_id = str(payload.get("logical_thread_id") or "")
        logical_intent_id = _logical_thread_intent_id(logical_thread_id)
        if not inferred_intent and logical_intent_id:
            inferred_intent = logical_intent_id
        if inferred_intent == "unknown" and inferred_intent not in intents_by_id:
            inferred_intent = ""
            if logical_thread_id.endswith(":unknown"):
                logical_thread_id = ""
        if not inferred_intent and task_type == "bootstrap" and bootstrap_intent_id:
            inferred_intent = bootstrap_intent_id
        if not inferred_intent and task_type == "explore":
            inferred_intent = _infer_intent_from_worker_window(intent_windows, row["worker"] or "", row["created_at"] or "")
        attempt_id = str(payload.get("attempt_id") or "")
        attempt_role = str(payload.get("attempt_role") or phase)

        if not logical_thread_id:
            if task_type == "maintainer":
                if not inferred_intent:
                    continue
                logical_thread_id = f"maintainer:{project_id}:{inferred_intent}"
            elif task_type in {"reason", "plan"}:
                if reason_cycle_closed and msg == "phase started":
                    reason_cycle += 1
                    reason_cycle_closed = False
                logical_thread_id = f"reason:{project_id}:{reason_cycle}"
            elif inferred_intent:
                logical_thread_id = f"intent:{project_id}:{inferred_intent}"
            else:
                logical_thread_id = ""

        if not logical_thread_id and task_type in {"explore", "maintainer"}:
            # Historical events emitted before logical_thread_id/intent_id existed cannot be
            # assigned safely. Do not merge them into a fake i002/unknown thread.
            continue

        if task_type == "maintainer":
            maintainer_intent = inferred_intent or _logical_thread_intent_id(logical_thread_id)
            if not maintainer_intent:
                continue
            logical_thread_id = f"maintainer:{project_id}:{maintainer_intent}"
            label = _conversation_group_label("m", maintainer_intent, len([t for t in threads.values() if t.get("type") == "maintainer"]) + 1)
            thread = ensure_thread(logical_thread_id, "maintainer", label, subtitle=f"Maintainer 漏洞管理{(' · ' + maintainer_intent) if maintainer_intent else ''}", intent=maintainer_intent or "")
        elif task_type in {"reason", "plan"} or logical_thread_id.startswith("reason:"):
            label = _conversation_group_label("r", logical_thread_id.rsplit(":", 1)[-1], reason_cycle)
            logical_thread_id = f"reason:{project_id}:{label[1:]}"
            thread = ensure_thread(logical_thread_id, "reason", label, subtitle="Reason 推理/计划")
        elif inferred_intent or logical_thread_id.startswith("intent:"):
            intent_label = inferred_intent or _logical_thread_intent_id(logical_thread_id) or "unknown"
            logical_thread_id = f"intent:{project_id}:{intent_label}"
            thread = ensure_intent_thread(intent_label)
        else:
            continue

        active_key = (thread["id"], phase, str(row["worker"] or ""))
        if msg == "phase started":
            if not attempt_id:
                attempt_id = f"{thread['id']}:{phase}:{row['worker']}:{row['id']}"
            active_attempts[active_key] = attempt_id
            attempt = ensure_attempt(attempt_id, thread, row_dict, payload, attempt_role)
            attempt["status"] = "running"
        elif msg == "phase finished":
            attempt_id = attempt_id or active_attempts.get(active_key) or f"{thread['id']}:{phase}:{row['worker']}:{row['id']}"
            attempt = ensure_attempt(attempt_id, thread, row_dict, payload, attempt_role)
            timed_out = bool(payload.get("timed_out"))
            cancelled = bool(payload.get("cancelled"))
            returncode = payload.get("returncode")
            attempt["finished_at"] = row["created_at"]
            attempt["duration_ms"] = _duration_ms(attempt.get("started_at"), row["created_at"])
            attempt["status"] = "timeout" if timed_out else ("cancelled" if cancelled else ("success" if returncode in (0, None) and row["level"] != "warn" else "failed"))
            attempt["diagnostic"] = _timeout_diagnostic(attempt, payload) if timed_out else {}
            if payload.get("token_usage"):
                attempt["token_usage"] = payload.get("token_usage")
            if attempt["status"] == "timeout":
                thread["timeout_count"] += 1
            if attempt["status"] in {"timeout", "failed", "cancelled"}:
                thread["error_count"] += 1
            active_attempts.pop(active_key, None)
        elif msg == "token usage":
            attempt_id = attempt_id or active_attempts.get(active_key) or ""
            attempt = attempts.get(attempt_id) if attempt_id else None
            if attempt is not None and payload.get("token_usage"):
                attempt["token_usage"] = payload.get("token_usage")
        else:
            attempt_id = attempt_id or active_attempts.get(active_key) or ""

        if task_type == "reason" and msg == "phase finished" and row["level"] != "warn" and not payload.get("timed_out"):
            reason_cycle_closed = True

        if task_type == "reason" and msg == "created intents":
            created_intents = payload.get("created_intents") or []
            if isinstance(created_intents, list):
                thread["dispatched_intents"] = [item for item in created_intents if isinstance(item, dict)]
                if thread["dispatched_intents"]:
                    thread["summary"] = "派发 " + ", ".join(str(item.get("id") or "") for item in thread["dispatched_intents"][:8] if item.get("id"))

        item_kind = "timeout" if payload.get("timed_out") else ("token" if msg == "token usage" else "system")
        append_item({
            "id": f"event:{row['id']}",
            "thread_id": thread["id"],
            "attempt_id": attempt_id,
            "kind": item_kind,
            "task_type": task_type,
            "phase": phase,
            "worker": row["worker"],
            "intent_id": inferred_intent or thread.get("intent_id") or "",
            "level": row["level"],
            "status": attempts.get(attempt_id, {}).get("status", "") if attempt_id else "",
            "title": _event_title(msg, task_type, phase, row["level"], payload),
            "content": _event_content(msg, payload),
            "payload": _safe_payload(payload),
            "created_at": row["created_at"],
            "diagnostic": _timeout_diagnostic(attempts.get(attempt_id, {}), payload) if payload.get("timed_out") else {},
        })

    if include_items:
        target_attempts = _conversation_target_attempts(attempts, thread_id=thread_id, intent_id=intent_id)
        _append_session_items(project_id, target_attempts, append_item)
        _append_tool_items(tool_rows, target_attempts, append_item, strict_time=bool(thread_id or intent_id))

    for row in intent_rows:
        ensure_intent_thread(str(row["id"]))

    for thread in threads.values():
        statuses = [attempt.get("status") for attempt in thread["attempts"]]
        if thread.get("type") == "intent" and thread.get("business_status"):
            thread["status"] = thread["business_status"]
        elif any(status == "running" for status in statuses):
            thread["status"] = "running"
        elif any(status == "success" for status in statuses):
            thread["status"] = "success" if not thread["timeout_count"] else "recovered"
        elif any(status == "timeout" for status in statuses):
            thread["status"] = "timeout"
        elif any(status == "failed" for status in statuses):
            thread["status"] = "failed"
        else:
            thread["status"] = "pending" if thread.get("type") == "intent" else "unknown"
        if thread["timeout_count"] and len(thread["attempts"]) > thread["timeout_count"]:
            thread["summary"] = f"{thread['timeout_count']} 次超时，后续 worker 已接替"
        elif thread["timeout_count"]:
            thread["summary"] = f"{thread['timeout_count']} 次超时，未见成功接替"
        elif thread["attempt_count"]:
            thread["summary"] = f"{thread['attempt_count']} 次尝试 · {', '.join(thread['workers'][:3])}"
        elif thread.get("type") == "intent":
            bits = [thread.get("intent_id") or ""]
            if thread.get("fact_id"):
                bits.append(f"-> {thread['fact_id']}")
            thread["summary"] = " ".join([bit for bit in bits if bit]) or thread.get("subtitle", "")

    for index, thread in enumerate(
        sorted(
            [item for item in threads.values() if item.get("type") == "maintainer"],
            key=lambda item: (item.get("started_at") or "", item.get("id") or ""),
        ),
        1,
    ):
        thread["label"] = f"m{index:03d}"
        intent_id_for_thread = thread.get("intent_id") or ""
        intent_row = intents_by_id.get(intent_id_for_thread)
        if intent_row is not None:
            thread["fact_id"] = intent_row["to_fact_id"] or ""
            thread["description"] = intent_row["description"] or ""
            thread["related"] = {
                "intent_id": intent_id_for_thread,
                "fact_id": intent_row["to_fact_id"] or "",
                "description": intent_row["description"] or "",
            }
            thread["summary"] = " · ".join([part for part in [intent_id_for_thread, thread.get("fact_id")] if part]) or thread.get("summary", "")

    reason_threads = sorted(
        [item for item in threads.values() if item.get("type") == "reason"],
        key=lambda item: (item.get("started_at") or "", item.get("id") or ""),
    )
    for idx, thread in enumerate(reason_threads):
        start = thread.get("started_at") or ""
        end = reason_threads[idx + 1].get("started_at") if idx + 1 < len(reason_threads) else "9999"
        if thread.get("dispatched_intents"):
            continue
        dispatched = []
        for row in intent_rows:
            created_at = row["created_at"] or ""
            if start and start <= created_at < end:
                dispatched.append({
                    "id": row["id"],
                    "fact_id": row["to_fact_id"] or "",
                    "description": row["description"] or "",
                })
        thread["dispatched_intents"] = dispatched
        if dispatched:
            thread["summary"] = "派发 " + ", ".join(item["id"] for item in dispatched[:8])

    for thread in threads.values():
        groups: dict[str, dict] = {}
        for attempt in thread.get("attempts", []):
            name = attempt.get("worker") or "unknown"
            group = groups.setdefault(name, {"worker": name, "attempts": [], "status": "unknown", "item_count": 0, "token_usage": {}})
            group["attempts"].append(attempt)
            group["item_count"] += int(attempt.get("item_count") or 0)
            if attempt.get("token_usage"):
                group["token_usage"] = _sum_token_usage(group.get("token_usage") or {}, attempt.get("token_usage") or {})
        for group in groups.values():
            statuses = [attempt.get("status") for attempt in group["attempts"]]
            group["status"] = _aggregate_status(statuses)
        thread["worker_groups"] = sorted(groups.values(), key=lambda item: item["worker"])

    requested_thread_type = thread_type if thread_type in {"intent", "reason", "maintainer"} else ""
    if thread_id:
        visible_thread_ids = {thread_id}
    elif intent_id:
        visible_thread_ids = {f"intent:{project_id}:{intent_id}"}
    else:
        visible_thread_ids = {
            thread["id"]
            for thread in threads.values()
            if not requested_thread_type or thread.get("type") == requested_thread_type
        }
    if include_items:
        filtered_items = _filter_conversation_items(items, thread_id=thread_id, intent_id=intent_id, worker=worker, kind=kind, query=query)
        filtered_items = [item for item in filtered_items if item.get("thread_id") in visible_thread_ids]
        filtered_items.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
        if not any([thread_id, intent_id, worker, kind, query]):
            filtered_items = _balanced_conversation_items(filtered_items, visible_thread_ids, limit)
        else:
            filtered_items = filtered_items[-limit:]
    else:
        filtered_items = []
    thread_list = sorted(
        [thread for thread in threads.values() if thread.get("id") in visible_thread_ids],
        key=lambda thread: (_conversation_type_sort(thread.get("type")), thread.get("label") or "", thread.get("started_at") or ""),
    )
    stats = {
        "thread_count": len(thread_list),
        "item_count": len(items),
        "timeout_count": sum(1 for item in items if item.get("kind") == "timeout"),
        "workers": sorted({item.get("worker") for item in items if item.get("worker")}),
        "intents": sorted({thread.get("intent_id") for thread in thread_list if thread.get("intent_id")}),
        "thread_types": {
            "intent": sum(1 for thread in threads.values() if thread.get("type") == "intent"),
            "reason": sum(1 for thread in threads.values() if thread.get("type") == "reason"),
            "maintainer": sum(1 for thread in threads.values() if thread.get("type") == "maintainer"),
        },
    }
    return {
        "project_id": project_id,
        "threads": thread_list,
        "items": filtered_items,
        "stats": stats,
        "filters": {"thread_id": thread_id, "thread_type": requested_thread_type, "intent_id": intent_id, "worker": worker, "kind": kind, "q": query},
    }


def _json_dict(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _balanced_conversation_items(items: list[dict], visible_thread_ids: set[str], limit: int) -> list[dict]:
    if not items or not visible_thread_ids:
        return items[-limit:]
    by_thread: dict[str, list[dict]] = {thread_id: [] for thread_id in visible_thread_ids}
    for item in items:
        thread_id = item.get("thread_id") or ""
        if thread_id in by_thread:
            by_thread[thread_id].append(item)
    per_thread = max(3, min(40, limit // max(len(visible_thread_ids), 1) or 1))
    selected = []
    for thread_items in by_thread.values():
        if thread_items:
            selected.extend(thread_items[-per_thread:])
    selected.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
    return selected


def _conversation_target_attempts(attempts: dict[str, dict], *, thread_id: str, intent_id: str) -> dict[str, dict]:
    if not thread_id and not intent_id:
        return attempts
    result = {}
    for attempt_id, attempt in attempts.items():
        if thread_id and attempt.get("thread_id") != thread_id:
            continue
        if intent_id and attempt.get("intent_id") != intent_id:
            continue
        result[attempt_id] = attempt
    return result


def _project_bootstrap_intent_id(conn, project_id: str) -> str:
    try:
        row = conn.execute(
            """
            SELECT id FROM intents
            WHERE project_id = ? AND creator = 'dispatcher.bootstrap'
            ORDER BY created_at, id LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    except Exception:
        return ""
    return str(row["id"] or "") if row else ""


def _logical_thread_intent_id(logical_thread_id: str) -> str:
    if not logical_thread_id:
        return ""
    parts = logical_thread_id.split(":")
    if len(parts) >= 3 and parts[0] in {"intent", "maintainer"}:
        return parts[-1]
    return ""


def _conversation_intent_windows(intent_rows) -> list[dict]:
    windows = []
    for row in intent_rows:
        windows.append(
            {
                "id": str(row["id"] or ""),
                "worker": str(row["worker"] or ""),
                "started_at": str(row["created_at"] or ""),
                "finished_at": str(row["concluded_at"] or ""),
            }
        )
    return windows


def _infer_intent_from_worker_window(intent_windows: list[dict], worker: str, created_at: str) -> str:
    if not worker or not created_at:
        return ""
    matches = []
    for window in intent_windows:
        if window.get("worker") != worker:
            continue
        started_at = window.get("started_at") or ""
        finished_at = window.get("finished_at") or ""
        if started_at and created_at < started_at:
            continue
        if finished_at and created_at > finished_at:
            continue
        matches.append(window)
    if not matches:
        return ""
    return sorted(matches, key=lambda item: item.get("started_at") or "")[-1].get("id") or ""


def _conversation_group_label(prefix: str, value: str | int | None, fallback_seq: int = 1) -> str:
    text = str(value or "").strip()
    if text.startswith(prefix) and text[1:].isdigit():
        return f"{prefix}{int(text[1:]):03d}"
    if text.isdigit():
        return f"{prefix}{int(text):03d}"
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return f"{prefix}{int(digits):03d}"
    return f"{prefix}{fallback_seq:03d}"


def _conversation_type_sort(thread_type: str | None) -> int:
    return {"intent": 0, "reason": 1, "maintainer": 2}.get(str(thread_type or ""), 9)


def _aggregate_status(statuses: list[str]) -> str:
    values = [status for status in statuses if status]
    if not values:
        return "unknown"
    if any(status == "running" for status in values):
        return "running"
    if any(status == "success" for status in values):
        return "success" if not any(status in {"timeout", "failed", "cancelled"} for status in values) else "recovered"
    if any(status == "timeout" for status in values):
        return "timeout"
    if any(status == "failed" for status in values):
        return "failed"
    if any(status == "cancelled" for status in values):
        return "cancelled"
    return values[-1]


def _sum_token_usage(left: dict, right: dict) -> dict:
    result = dict(left or {})
    for key, value in (right or {}).items():
        try:
            result[key] = int(result.get(key) or 0) + int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def _safe_payload(payload: dict) -> dict:
    hidden = {"command_preview"}
    result = {}
    for key, value in payload.items():
        if key in hidden:
            continue
        result[key] = value
    return result


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _duration_ms(start: str | None, end: str | None) -> int | None:
    started = _parse_time(start)
    finished = _parse_time(end)
    if started is None or finished is None:
        return None
    return max(0, int((finished - started).total_seconds() * 1000))


def _event_title(message: str, task_type: str, phase: str, level: str, payload: dict) -> str:
    if payload.get("timed_out"):
        return f"{phase} 超时"
    if message == "phase started":
        return f"{phase} 开始"
    if message == "phase finished":
        return f"{phase} 完成" if level != "warn" else f"{phase} 异常结束"
    if message == "token usage":
        usage = payload.get("token_usage") or {}
        total = usage.get("total_tokens") or usage.get("total") or 0
        return f"Token {int(total):,}" if total else "Token usage"
    return message or task_type


def _event_content(message: str, payload: dict) -> str:
    if payload.get("stdout_preview"):
        return str(payload.get("stdout_preview") or "")
    if payload.get("stderr_preview"):
        return str(payload.get("stderr_preview") or "")
    if payload.get("intent_description"):
        return str(payload.get("intent_description") or "")
    return message


def _timeout_diagnostic(attempt: dict, payload: dict) -> dict:
    timeout_seconds = payload.get("timeout_seconds") or attempt.get("timeout_seconds")
    phase = attempt.get("phase") or ""
    suggestions = []
    if "conclude" in phase:
        suggestions.append("conclude 超时：优先检查 execute 是否已有足够工具结果，可尝试简化总结或增加 conclude timeout")
    elif "explore" in phase:
        suggestions.append("explore 超时：若工具仍在推进，增加 timeout；若工具过多，拆分 intent 或换 worker")
    elif "maintainer" in phase:
        suggestions.append("maintainer 超时：考虑分批入库或提高 maintainer timeout")
    elif "reason" in phase:
        suggestions.append("reason 超时：降低 max_intents、减少上下文或提高 reason timeout")
    suggestions.append("查看该 thread 后续 attempt，确认是否已由其他 worker 接替")
    return {
        "timeout_seconds": timeout_seconds,
        "returncode": payload.get("returncode"),
        "stdout_preview": payload.get("stdout_preview") or "",
        "stderr_preview": payload.get("stderr_preview") or "",
        "suggestions": suggestions,
    }


def _append_session_items(project_id: str, attempts: dict[str, dict], append_item) -> None:
    used_paths: set[Path] = set()
    for attempt in list(attempts.values()):
        session_id = attempt.get("session_id") or ""
        worker = attempt.get("worker") or ""
        if not worker:
            continue
        session_dir = Path("/tmp/axiom-pi") / worker / "sessions"
        if not session_dir.is_dir():
            continue
        if session_id:
            matches = sorted(session_dir.glob(f"*{session_id}*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        else:
            inferred = _find_session_for_attempt(session_dir, attempt, used_paths)
            matches = [inferred] if inferred is not None else []
            if inferred is not None:
                session_id = _session_id_from_path(inferred)
                attempt["session_id"] = session_id
        if not matches:
            continue
        used_paths.add(matches[0])
        for index, item in enumerate(_parse_session_conversation(matches[0], attempt)):
            item["id"] = f"session:{session_id}:{index}:{item['kind']}"
            append_item(item)


def _find_session_for_attempt(session_dir: Path, attempt: dict, used_paths: set[Path]) -> Path | None:
    started = _parse_time(attempt.get("started_at"))
    if started is None:
        return None
    finished = _parse_time(attempt.get("finished_at"))
    candidates = []
    for path in session_dir.glob("*.jsonl"):
        if path in used_paths:
            continue
        session_time = _session_timestamp(path)
        if session_time is None:
            continue
        # Historical rows may lack session_id. The session file is created when
        # the worker starts, so use a small tolerance around the attempt window.
        if session_time < started - timedelta(seconds=15):
            continue
        if finished is not None and session_time > finished + timedelta(seconds=15):
            continue
        candidates.append((abs((session_time - started).total_seconds()), path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def _session_timestamp(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = _json_dict(handle.readline())
    except OSError:
        return None
    return _parse_time(first.get("timestamp"))


def _session_id_from_path(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = _json_dict(handle.readline())
    except OSError:
        return path.stem
    candidate = first.get("id")
    return str(candidate or path.stem)


def _parse_session_conversation(path: Path, attempt: dict) -> list[dict]:
    items: list[dict] = []
    created_at = attempt.get("started_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = _json_dict(line)
                if event.get("type") != "message":
                    continue
                message = event.get("message") or {}
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                thinking = _extract_session_thinking(message)
                if thinking:
                    items.append(_conversation_item(attempt, "thinking", "思考", _truncate_conversation(thinking), created_at))
                text = _extract_session_text(message)
                if text:
                    items.append(_conversation_item(attempt, "assistant", "回复", _truncate_conversation(text), created_at))
                usage = message.get("usage") or {}
                if isinstance(usage, dict) and usage:
                    items.append(_conversation_item(attempt, "token", "Session token usage", "", created_at, payload={"token_usage": usage}))
    except OSError:
        return []
    return items


def _conversation_item(attempt: dict, kind: str, title: str, content: str, created_at: str, payload: dict | None = None) -> dict:
    return {
        "thread_id": attempt.get("thread_id") or "",
        "attempt_id": attempt.get("id") or "",
        "kind": kind,
        "task_type": attempt.get("task_type") or "",
        "phase": attempt.get("phase") or "",
        "worker": attempt.get("worker") or "",
        "intent_id": attempt.get("intent_id") or "",
        "level": "info",
        "status": attempt.get("status") or "",
        "title": title,
        "content": content,
        "payload": payload or {},
        "created_at": created_at,
        "diagnostic": {},
    }


def _extract_session_thinking(message: dict) -> str:
    for key in ("thinking", "reasoning_content", "reasoningContent"):
        value = message.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "off":
            return value.strip()
    content = message.get("content") or []
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and str(block.get("type", "")).lower() in {"thinking", "reasoning"}:
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    return ""


def _extract_session_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    return ""


def _append_tool_items(tool_rows, attempts: dict[str, dict], append_item, *, strict_time: bool = False) -> None:
    attempt_list = list(attempts.values())
    for row in tool_rows:
        created_at = row["created_at"]
        attempt = attempts.get(row["attempt_id"] or "") if "attempt_id" in row.keys() else None
        if attempt is None:
            attempt = _find_attempt_for_time(attempt_list, created_at, allow_before=not strict_time)
        if attempt is None:
            continue
        argv = []
        try:
            argv = json.loads(row["argv_json"] or "[]")
        except Exception:
            argv = []
        content = "\n".join(str(part) for part in argv[1:]) if len(argv) > 1 else ""
        append_item({
            "id": f"tool:{row['id']}",
            "thread_id": attempt.get("thread_id") or "",
            "attempt_id": attempt.get("id") or "",
            "kind": "tool",
            "task_type": attempt.get("task_type") or "",
            "phase": attempt.get("phase") or "",
            "worker": (row["worker"] if "worker" in row.keys() else "") or attempt.get("worker") or "",
            "intent_id": (row["intent_id"] if "intent_id" in row.keys() else "") or attempt.get("intent_id") or "",
            "level": "warn" if row["status"] == "error" else "info",
            "status": row["status"],
            "title": f"工具 {row['tool']}",
            "content": _truncate_conversation(content),
            "payload": {
                "tool": row["tool"],
                "argv": argv,
                "stdout": _truncate_conversation(row["stdout"] or ""),
                "stderr": _truncate_conversation(row["stderr"] or ""),
                "duration_ms": row["duration_ms"],
                "returncode": row["returncode"],
            },
            "created_at": created_at,
            "diagnostic": {},
        })


def _find_attempt_for_time(attempts: list[dict], created_at: str, *, allow_before: bool = True) -> dict | None:
    current = _parse_time(created_at)
    if current is None:
        return None
    candidates = []
    for attempt in attempts:
        started = _parse_time(attempt.get("started_at"))
        finished = _parse_time(attempt.get("finished_at"))
        if started is None:
            continue
        if current >= started and (finished is None or current <= finished):
            candidates.append(attempt)
    if candidates:
        return sorted(candidates, key=lambda item: item.get("started_at") or "")[-1]
    if not allow_before:
        return None
    before = [attempt for attempt in attempts if (attempt.get("started_at") or "") <= created_at]
    return sorted(before, key=lambda item: item.get("started_at") or "")[-1] if before else None


def _filter_conversation_items(items: list[dict], *, thread_id: str, intent_id: str, worker: str, kind: str, query: str) -> list[dict]:
    query_lower = query.lower()
    result = []
    for item in items:
        if thread_id and item.get("thread_id") != thread_id:
            continue
        if intent_id and item.get("intent_id") != intent_id:
            continue
        if worker and item.get("worker") != worker:
            continue
        if kind and item.get("kind") != kind:
            continue
        if query_lower:
            haystack = " ".join(str(item.get(key) or "") for key in ("title", "content", "worker", "phase", "intent_id")).lower()
            if query_lower not in haystack:
                continue
        result.append(item)
    return result


def _truncate_conversation(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _project_group_summary_from_row(row) -> ProjectGroupSummary:
    return ProjectGroupSummary(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        created_at=row["created_at"],
        task_count=row["task_count"],
        active_task_count=row["active_task_count"],
        stopped_task_count=row["stopped_task_count"],
        completed_task_count=row["completed_task_count"],
        vuln_count=row["vuln_count"],
        blackboard_count=row["blackboard_count"],
    )


def _default_project_group_id(conn) -> str:
    row = conn.execute("SELECT id FROM project_groups ORDER BY created_at, id LIMIT 1").fetchone()
    if row is not None:
        return row["id"]
    group_id = next_project_group_id(conn)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO project_groups (id, title, description, status, created_at)
        VALUES (?, '默认项目', '自动创建的默认项目', 'active', ?)
        """,
        (group_id, now),
    )
    return group_id


@router.get("/project-groups", response_model=list[ProjectGroupSummary])
def list_project_groups():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT g.*,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id) AS task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'active') AS active_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'stopped') AS stopped_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'completed') AS completed_task_count,
                (SELECT COUNT(*) FROM vulns v JOIN projects p ON p.id = v.project_id WHERE p.parent_project_id = g.id) AS vuln_count,
                (SELECT COUNT(*) FROM blackboard_items b WHERE b.group_id = g.id) AS blackboard_count
            FROM project_groups g
            ORDER BY g.created_at, g.id
            """
        ).fetchall()
        return [
            _project_group_summary_from_row(row)
            for row in rows
        ]


@router.post("/project-groups", response_model=ProjectGroupSummary, status_code=201)
def create_project_group(body: CreateProjectGroupRequest):
    with get_conn() as conn:
        group_id = next_project_group_id(conn)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO project_groups (id, title, description, status, created_at)
            VALUES (?, ?, ?, 'active', ?)
            """,
            (
                group_id,
                body.title,
                body.description,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT g.*,
                0 AS task_count,
                0 AS active_task_count,
                0 AS stopped_task_count,
                0 AS completed_task_count,
                0 AS vuln_count,
                0 AS blackboard_count
            FROM project_groups g
            WHERE g.id = ?
            """,
            (group_id,),
        ).fetchone()
        assert row is not None
        return _project_group_summary_from_row(row)


@router.put("/project-groups/{group_id}", response_model=ProjectGroupSummary)
def update_project_group(group_id: str, body: UpdateProjectGroupRequest):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        conn.execute(
            "UPDATE project_groups SET title = ?, description = ?, status = ? WHERE id = ?",
            (body.title, body.description, body.status, group_id),
        )
        row = conn.execute(
            """
            SELECT g.*,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id) AS task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'active') AS active_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'stopped') AS stopped_task_count,
                (SELECT COUNT(*) FROM projects p WHERE p.parent_project_id = g.id AND p.status = 'completed') AS completed_task_count,
                (SELECT COUNT(*) FROM vulns v JOIN projects p ON p.id = v.project_id WHERE p.parent_project_id = g.id) AS vuln_count,
                (SELECT COUNT(*) FROM blackboard_items b WHERE b.group_id = g.id) AS blackboard_count
            FROM project_groups g
            WHERE g.id = ?
            """,
            (group_id,),
        ).fetchone()
        assert row is not None
        return _project_group_summary_from_row(row)


@router.delete("/project-groups/{group_id}", status_code=204)
def delete_project_group(group_id: str):
    """Delete a project group and cascade-delete all its tasks, facts, intents, vulns, blackboard items."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM project_groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Project group {group_id} not found")
        project_rows = conn.execute(
            "SELECT id FROM projects WHERE parent_project_id = ?", (group_id,)
        ).fetchall()
        project_ids = [r["id"] for r in project_rows]
        for pid in project_ids:
            conn.execute("DELETE FROM tool_executions WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM task_events WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM vulns WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM hints WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM intent_sources WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM intents WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM facts WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM blackboard_items WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        conn.execute("DELETE FROM blackboard_items WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM crawler_pages WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM crawler_apis WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM crawler_page_maps WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM project_groups WHERE id = ?", (group_id,))


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        rows = conn.execute("""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            ORDER BY p.created_at
        """).fetchall()
        return [
            ProjectSummary(
                **project_meta_from_row(row).model_dump(),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()
        parent_project_id = body.parent_project_id or _default_project_group_id(conn)
        get_project_group_or_404(conn, parent_project_id)
        task_scope = body.task_scope or TaskScope(enabled=False)
        task_scope_enabled, task_scope_json = _scope_to_storage(task_scope)
        conn.execute(
            """INSERT INTO projects (
                id, parent_project_id, title, status, bootstrap_enabled, kk_report_enabled, depth_mode, guard_enabled,
                worker_pool, task_scope_enabled, task_scope_json, credential_headers, created_at
            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, parent_project_id, body.title, body.bootstrap_enabled, 1 if body.kk_report_enabled else 0,
                1 if body.depth_mode else 0, 1 if body.guard_enabled else 0, json.dumps(body.worker_pool or []),
                task_scope_enabled, task_scope_json, body.credential_headers, now,
            ),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        project_row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return ProjectDetail(
            project=project_meta_from_row(project_row),
            facts=[Fact(id="origin", description=body.origin), Fact(id="goal", description=body.goal)],
            intents=[],
            hints=hints,
            blackboard=[],
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(
    project_id: str,
    intent_id: str = Query(default=""),
    fact_id: str = Query(default=""),
):
    intent_id = intent_id.strip()
    fact_id = fact_id.strip()
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        if fact_id:
            facts = [row for row in facts if row["id"] == fact_id]
            if not facts:
                raise HTTPException(404, "Fact not found")
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        intents = build_intents(conn, project_id)
        if intent_id:
            intents = [item for item in intents if item.id == intent_id]
            if not intents:
                raise HTTPException(404, "Intent not found")

        fact_models = []
        for f in facts:
            fact_payload = dict(f)
            fact_payload["status"] = "archived" if bool(f["archived"]) else "active"
            fact_payload["lookup_path"] = f"/projects/{project_id}?fact_id={f['id']}"
            fact_models.append(Fact(**fact_payload))
        intent_models = []
        for item in intents:
            payload = item.model_dump(by_alias=True)
            payload["status"] = "concluded" if item.concluded_at else "open"
            payload["lookup_path"] = f"/projects/{project_id}?intent_id={item.id}"
            intent_models.append(Intent.model_validate(payload))

        # Filter mode returns only explicitly requested resources.
        # Avoid returning large unrelated sections (e.g. all vulns) for pinpoint queries.
        is_filtered_query = bool(intent_id or fact_id)
        if intent_id and not fact_id:
            fact_models = []
        if fact_id and not intent_id:
            intent_models = []

        group_id = row["parent_project_id"] or ""
        return ProjectDetail(
            project=project_meta_from_row(row),
            facts=fact_models,
            intents=intent_models,
            hints=[] if is_filtered_query else [Hint(**dict(h)) for h in hints],
            vulns=[] if is_filtered_query else build_vulns(conn, project_id),
            blackboard=[] if is_filtered_query else (build_blackboard_items_for_group(conn, group_id) if group_id else []),
        )


@router.get("/projects/{project_id}/maintainer-status")
def get_maintainer_status(project_id: str):
    """Return intent IDs currently being processed by maintainer workers."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        # Find intents that have a maintainer 'phase started' event in the last 5 minutes
        # but no corresponding 'phase finished' event
        started = set()
        finished = set()
        rows = conn.execute(
            "SELECT message, payload_json FROM task_events WHERE project_id = ? AND task_type = 'maintainer' AND created_at > datetime('now', '-5 minutes') ORDER BY id DESC",
            (project_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            intent_id = payload.get("intent_id") or ""
            if row["message"] == "phase finished":
                finished.add(intent_id)
            elif "started" in (row["message"] or ""):
                started.add(intent_id)
        active = [iid for iid in started if iid and iid not in finished]
        return {"maintaining_intent_ids": active}


@router.get("/projects/{project_id}/events", response_model=list[TaskEvent])
def list_project_events(project_id: str, after_id: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT id, project_id, task_type, phase, worker, level, message, payload_json, created_at
            FROM task_events
            WHERE project_id = ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (project_id, after_id, limit),
        ).fetchall()
        events: list[TaskEvent] = []
        for row in rows:
            payload = {}
            raw_payload = row["payload_json"] or "{}"
            try:
                parsed = json.loads(raw_payload)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"raw_payload": raw_payload}
            events.append(
                TaskEvent(
                    id=row["id"],
                    project_id=row["project_id"],
                    task_type=row["task_type"],
                    phase=row["phase"],
                    worker=row["worker"],
                    level=row["level"],
                    message=row["message"],
                    payload=payload,
                    created_at=row["created_at"],
                )
            )
        return events


@router.get("/projects/{project_id}/conversation")
def get_project_conversation(
    project_id: str,
    thread_id: str = "",
    thread_type: str = "",
    intent_id: str = "",
    worker: str = "",
    kind: str = "",
    q: str = "",
    limit: int = Query(default=800, ge=1, le=3000),
):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return _build_conversation_payload(
            conn,
            project_id,
            thread_id=thread_id.strip(),
            thread_type=thread_type.strip(),
            intent_id=intent_id.strip(),
            worker=worker.strip(),
            kind=kind.strip(),
            query=q.strip(),
            limit=limit,
        )


@router.get("/projects/{project_id}/conversation/index")
def get_project_conversation_index(project_id: str, thread_type: str = ""):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        payload = _build_conversation_payload(conn, project_id, thread_type=thread_type.strip(), limit=3000, include_items=False)
        return {
            "project_id": project_id,
            "threads": payload["threads"],
            "stats": payload["stats"],
            "filters": payload.get("filters") or {},
        }


@router.post("/projects/{project_id}/events", response_model=TaskEvent, status_code=201)
def create_project_event(project_id: str, body: TaskEventCreateRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        now = utcnow()
        payload_json = json.dumps(body.payload, ensure_ascii=False, separators=(",", ":"))
        cur = conn.execute(
            """
            INSERT INTO task_events (project_id, task_type, phase, worker, level, message, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                body.task_type,
                body.phase,
                body.worker,
                body.level,
                body.message,
                payload_json,
                now,
            ),
        )
        event_id = int(cur.lastrowid)
        return TaskEvent(
            id=event_id,
            project_id=project_id,
            task_type=body.task_type,
            phase=body.phase,
            worker=body.worker,
            level=body.level,
            message=body.message,
            payload=body.payload,
            created_at=now,
        )


@router.get("/projects/{project_id}/vulns", response_model=list[Vulnerability])
def list_project_vulns(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_vulns(conn, project_id)


@router.post("/projects/{project_id}/vulns", response_model=Vulnerability, status_code=201)
def create_project_vuln(project_id: str, body: CreateVulnerabilityRequest):
    with get_conn() as conn:
        now = utcnow()
        vuln_id = next_vuln_id(conn, project_id)
        effective_status = (body.status or "").strip() or "待验证"
        evidence_json = json.dumps(body.evidence, ensure_ascii=False, separators=(",", ":"))
        steps_json = json.dumps(body.reproduction_steps, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """
            INSERT INTO vulns (
                id, project_id, title, type, severity, description, prerequisites, impact,
                evidence_json, reproduction_steps_json, recommendation, affected_scope,
                status, status_reason, source, source_file, reported_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vuln_id,
                project_id,
                body.title,
                body.type,
                body.severity,
                body.description,
                body.prerequisites,
                body.impact,
                evidence_json,
                steps_json,
                body.recommendation,
                body.affected_scope,
                effective_status,
                body.status_reason,
                body.source,
                body.source_file,
                body.reported_by,
                now,
            ),
        )
        return Vulnerability(
            id=vuln_id,
            title=body.title,
            type=body.type,
            severity=body.severity,
            description=body.description,
            prerequisites=body.prerequisites,
            impact=body.impact,
            evidence=body.evidence,
            reproduction_steps=body.reproduction_steps,
            recommendation=body.recommendation,
            affected_scope=body.affected_scope,
            status=effective_status,
            status_reason=body.status_reason,
            source=body.source,
            source_file=body.source_file,
            reported_by=body.reported_by,
            created_at=now,
        )


@router.put("/projects/{project_id}/vulns/{vuln_id}", response_model=Vulnerability)
def update_project_vuln(project_id: str, vuln_id: str, body: CreateVulnerabilityRequest):
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM vulns WHERE id = ? AND project_id = ?", (vuln_id, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Vulnerability not found")
        effective_status = (body.status or "").strip() or "待验证"
        evidence_json = json.dumps(body.evidence, ensure_ascii=False, separators=(",", ":"))
        steps_json = json.dumps(body.reproduction_steps, ensure_ascii=False, separators=(",", ":"))
        conn.execute("""
            UPDATE vulns SET title=?, type=?, severity=?, description=?, prerequisites=?, impact=?,
            evidence_json=?, reproduction_steps_json=?, recommendation=?, affected_scope=?, status=?, status_reason=?, source=?, source_file=?, reported_by=?
            WHERE id=? AND project_id=?
        """, (body.title, body.type, body.severity, body.description, body.prerequisites, body.impact,
              evidence_json, steps_json, body.recommendation, body.affected_scope, effective_status,
              body.status_reason,
              body.source, body.source_file, body.reported_by,
              vuln_id, project_id))
    return Vulnerability(id=vuln_id, title=body.title, type=body.type, severity=body.severity,
                         description=body.description, prerequisites=body.prerequisites,
                         impact=body.impact, evidence=body.evidence,
                         reproduction_steps=body.reproduction_steps, recommendation=body.recommendation,
                         affected_scope=body.affected_scope, status=effective_status, status_reason=body.status_reason, source=body.source,
                         source_file=body.source_file, reported_by=body.reported_by, created_at=row["created_at"])


@router.delete("/projects/{project_id}/vulns/{vuln_id}", status_code=204)
def delete_project_vuln(project_id: str, vuln_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM vulns WHERE id = ? AND project_id = ?", (vuln_id, project_id)).fetchone()
        if not row:
            raise HTTPException(404, "Vulnerability not found")
        conn.execute("DELETE FROM vulns WHERE id = ? AND project_id = ?", (vuln_id, project_id))


# --- Group-level blackboard ---


@router.get("/project-groups/{group_id}/blackboard", response_model=list[BlackboardItem])
def list_group_blackboard_items(group_id: str):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        return build_blackboard_items_for_group(conn, group_id)


@router.post("/project-groups/{group_id}/blackboard", response_model=BlackboardItem, status_code=201)
def create_group_blackboard_item(group_id: str, body: CreateBlackboardItemRequest):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        item_id = next_blackboard_item_id_for_group(conn, group_id)
        now = utcnow()
        dummy_pid = conn.execute(
            "SELECT id FROM projects WHERE parent_project_id = ? LIMIT 1",
            (group_id,),
        ).fetchone()
        if dummy_pid is None:
            dummy_pid = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
        fallback_pid = dummy_pid["id"] if dummy_pid else "proj_000"
        conn.execute(
            "INSERT INTO blackboard_items (id, project_id, group_id, title, content, hint, category, status, priority,"
            " related_fact_id, related_intent_id, related_vuln_id, creator, updated_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, fallback_pid, group_id, body.title, body.content, body.hint,
             body.category, body.status, body.priority,
             body.related_fact_id, body.related_intent_id, body.related_vuln_id,
             body.creator, body.creator, now, now),
        )
        return BlackboardItem(
            id=item_id, title=body.title, content=body.content, hint=body.hint,
            category=body.category, status=body.status, priority=body.priority,
            related_fact_id=body.related_fact_id, related_intent_id=body.related_intent_id,
            related_vuln_id=body.related_vuln_id, creator=body.creator, updated_by=body.creator,
            created_at=now, updated_at=now,
        )


@router.get("/project-groups/{group_id}/blackboard/{item_id}", response_model=BlackboardItem)
def get_group_blackboard_item(group_id: str, item_id: str):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        row = conn.execute(
            "SELECT * FROM blackboard_items WHERE id = ? AND group_id = ?",
            (item_id, group_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Blackboard item not found")
        from axiom.server.services import _blackboard_item_from_row
        return _blackboard_item_from_row(row)


@router.put("/project-groups/{group_id}/blackboard/{item_id}", response_model=BlackboardItem)
def update_group_blackboard_item(group_id: str, item_id: str, body: UpdateBlackboardItemRequest):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        row = conn.execute(
            "SELECT * FROM blackboard_items WHERE id = ? AND group_id = ?",
            (item_id, group_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Blackboard item not found")
        now = utcnow()
        conn.execute(
            "UPDATE blackboard_items SET title = ?, content = ?, hint = ?, category = ?, status = ?, priority = ?,"
            " related_fact_id = ?, related_intent_id = ?, related_vuln_id = ?, updated_by = ?, updated_at = ?"
            " WHERE id = ? AND group_id = ?",
            (body.title, body.content, body.hint, body.category, body.status, body.priority,
             body.related_fact_id, body.related_intent_id, body.related_vuln_id,
             body.updated_by, now, item_id, group_id),
        )
        return BlackboardItem(
            id=item_id, title=body.title, content=body.content, hint=body.hint,
            category=body.category, status=body.status, priority=body.priority,
            related_fact_id=body.related_fact_id, related_intent_id=body.related_intent_id,
            related_vuln_id=body.related_vuln_id, creator=row["creator"], updated_by=body.updated_by,
            created_at=row["created_at"], updated_at=now,
        )


@router.delete("/project-groups/{group_id}/blackboard/{item_id}", status_code=204)
def delete_group_blackboard_item(group_id: str, item_id: str):
    with get_conn() as conn:
        get_project_group_or_404(conn, group_id)
        conn.execute(
            "DELETE FROM blackboard_items WHERE id = ? AND group_id = ?",
            (item_id, group_id),
        )


# --- Task-level blackboard (legacy) ---

@router.get("/projects/{project_id}/blackboard", response_model=list[BlackboardItem])
def list_project_blackboard_items(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_blackboard_items(conn, project_id)


@router.post("/projects/{project_id}/blackboard", response_model=BlackboardItem, status_code=201)
def create_project_blackboard_item(project_id: str, body: CreateBlackboardItemRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        item_id = next_blackboard_item_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO blackboard_items (
                id, project_id, title, content, hint, category, status, priority,
                related_fact_id, related_intent_id, related_vuln_id,
                creator, updated_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                project_id,
                body.title,
                body.content,
                body.hint,
                body.category,
                body.status,
                body.priority,
                body.related_fact_id,
                body.related_intent_id,
                body.related_vuln_id,
                body.creator,
                body.creator,
                now,
                now,
            ),
        )
        return BlackboardItem(
            id=item_id,
            title=body.title,
            content=body.content,
            hint=body.hint,
            category=body.category,
            status=body.status,
            priority=body.priority,
            related_fact_id=body.related_fact_id,
            related_intent_id=body.related_intent_id,
            related_vuln_id=body.related_vuln_id,
            creator=body.creator,
            updated_by=body.creator,
            created_at=now,
            updated_at=now,
        )


@router.put("/projects/{project_id}/blackboard/{item_id}", response_model=BlackboardItem)
def update_project_blackboard_item(project_id: str, item_id: str, body: UpdateBlackboardItemRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        updated = conn.execute(
            """
            UPDATE blackboard_items
            SET title = ?,
                content = ?,
                hint = ?,
                category = ?,
                status = ?,
                priority = ?,
                related_fact_id = ?,
                related_intent_id = ?,
                related_vuln_id = ?,
                updated_by = ?,
                updated_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                body.title,
                body.content,
                body.hint,
                body.category,
                body.status,
                body.priority,
                body.related_fact_id,
                body.related_intent_id,
                body.related_vuln_id,
                body.updated_by,
                now,
                item_id,
                project_id,
            ),
        )
        if updated.rowcount == 0:
            raise HTTPException(404, "Blackboard item not found")
        row = conn.execute(
            "SELECT * FROM blackboard_items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        ).fetchone()
        assert row is not None
        return BlackboardItem(
            id=row["id"],
            title=row["title"],
            content=row["content"],
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


@router.post("/projects/{project_id}/blackboard/batch/status", response_model=list[BlackboardItem])
def batch_update_blackboard_item_status(project_id: str, body: BlackboardBatchStatusUpdateRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        for item_id in body.ids:
            conn.execute(
                """
                UPDATE blackboard_items
                SET status = ?,
                    updated_by = ?,
                    updated_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (body.status, body.updated_by, now, item_id, project_id),
            )
        return build_blackboard_items(conn, project_id)


@router.delete("/projects/{project_id}/blackboard/{item_id}", status_code=204)
def delete_project_blackboard_item(project_id: str, item_id: str):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        deleted = conn.execute(
            "DELETE FROM blackboard_items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        )
        if deleted.rowcount == 0:
            raise HTTPException(404, "Blackboard item not found")


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "stopped":
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/worker-pool", response_model=UpdateProjectWorkerPoolResponse)
def update_project_worker_pool(project_id: str, body: UpdateProjectWorkerPoolRequest):
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id)
        if row["status"] == "completed":
            raise HTTPException(409, "Completed projects cannot change worker pool")
        worker_pool_json = json.dumps(body.worker_pool, ensure_ascii=False)
        conn.execute(
            "UPDATE projects SET worker_pool = ? WHERE id = ?",
            (worker_pool_json, project_id),
        )
        cursor = conn.execute(
            """
            UPDATE intents
            SET worker_pool = ?
            WHERE project_id = ?
              AND concluded_at IS NULL
              AND worker IS NULL
            """,
            (worker_pool_json, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return UpdateProjectWorkerPoolResponse(
            project=project_meta_from_row(updated),
            updated_unclaimed_intents=max(0, int(cursor.rowcount or 0)),
        )


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)

        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?)",
            (iid, project_id, body.description, body.worker, body.worker, now, now, now),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )
        conn.execute(
            """
            UPDATE projects
            SET status = 'completed',
                reason_worker = NULL,
                reason_trigger = NULL,
                reason_started_at = NULL,
                reason_last_heartbeat_at = NULL
            WHERE id = ?
            """,
            (project_id,),
        )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse, response_model_exclude_defaults=True)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )


@router.get("/projects/{project_id}/tool-executions")
def get_tool_executions(project_id: str, since: str = "", limit: int = 200):
    """Return tool executions for a project, ordered by time.

    Use ?since=<id> for incremental polling — only returns records
    created after the given ID.  Default limit 200 prevents OOM on
    very large projects.
    """
    with get_conn() as conn:
        if since:
            rows = conn.execute(
                """
                SELECT id, tool, kind, argv_json, status, returncode,
                       stdout, stderr, duration_ms, worker, task_type, phase,
                       logical_thread_id, attempt_id, intent_id, session_id,
                       created_at, finished_at
                FROM tool_executions
                WHERE project_id = ? AND rowid > (
                    SELECT rowid FROM tool_executions WHERE id = ? AND project_id = ?
                )
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (project_id, since, project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, tool, kind, argv_json, status, returncode,
                       stdout, stderr, duration_ms, worker, task_type, phase,
                       logical_thread_id, attempt_id, intent_id, session_id,
                       created_at, finished_at
                FROM tool_executions
                WHERE project_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
    return {
        "project_id": project_id,
        "items": [
            {
                "id": row["id"],
                "tool": row["tool"],
                "kind": row["kind"],
                "argv": json.loads(row["argv_json"] or "[]"),
                "status": row["status"],
                "returncode": row["returncode"],
                "stdout": row["stdout"],
                "stderr": row["stderr"],
                "duration_ms": row["duration_ms"],
                "worker": row["worker"],
                "task_type": row["task_type"],
                "phase": row["phase"],
                "logical_thread_id": row["logical_thread_id"],
                "attempt_id": row["attempt_id"],
                "intent_id": row["intent_id"],
                "session_id": row["session_id"],
                "created_at": row["created_at"],
                "finished_at": row["finished_at"],
            }
            for row in rows
        ],
    }


@router.get("/projects/{project_id}/skill-usage")
def get_skill_usage(project_id: str):
    """Count skill invocations by scanning read tool calls to SKILL.md files."""
    import re
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT argv_json, created_at
            FROM tool_executions
            WHERE project_id = ? AND tool = 'read' AND argv_json LIKE '%SKILL.md%'
            ORDER BY created_at ASC
            """,
            (project_id,),
        ).fetchall()
    pattern = re.compile(r'/skills/([^/]+)/SKILL\.md')
    stats: dict[str, dict] = {}
    for row in rows:
        argv = json.loads(row["argv_json"] or "[]")
        arg_str = " ".join(argv)
        m = pattern.search(arg_str)
        if not m:
            continue
        name = m.group(1)
        if name not in stats:
            stats[name] = {"skill": name, "count": 0, "last_used": row["created_at"]}
        stats[name]["count"] += 1
        stats[name]["last_used"] = row["created_at"]
    return {
        "project_id": project_id,
        "items": sorted(stats.values(), key=lambda x: x["count"], reverse=True),
        "total": sum(s["count"] for s in stats.values()),
    }


@router.get("/skill-usage")
def get_global_skill_usage():
    """Global skill invocation stats across all projects."""
    import re
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT argv_json, created_at, project_id
            FROM tool_executions
            WHERE tool = 'read' AND argv_json LIKE '%SKILL.md%'
            ORDER BY created_at ASC
            """,
        ).fetchall()
    pattern = re.compile(r'/skills/([^/]+)/SKILL\.md')
    stats: dict[str, dict] = {}
    for row in rows:
        argv = json.loads(row["argv_json"] or "[]")
        arg_str = " ".join(argv)
        m = pattern.search(arg_str)
        if not m:
            continue
        name = m.group(1)
        if name not in stats:
            stats[name] = {"skill": name, "count": 0, "last_used": row["created_at"]}
        stats[name]["count"] += 1
        stats[name]["last_used"] = row["created_at"]
    return {
        "items": sorted(stats.values(), key=lambda x: x["count"], reverse=True),
        "total": sum(s["count"] for s in stats.values()),
    }


@router.post("/upload-har")
async def upload_har(file: UploadFile):
    """Upload a HAR file. Saved to /tmp/axiom-uploads/, path returned for origin."""
    if not file.filename or not file.filename.endswith(('.har', '.json', '.txt')):
        raise HTTPException(400, "Only .har / .json / .txt accepted")
    upload_dir = "/tmp/axiom-uploads"
    _os.makedirs(upload_dir, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:12]}_{file.filename}"
    path = _os.path.join(upload_dir, safe_name)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    return {"path": path, "size": len(content)}


@router.post("/projects/{project_id}/explore/start", response_model=ExploreStatus, status_code=201)
def start_explore(project_id: str, body: ExploreStartRequest):
    """Create an explore intent for the project. The dispatcher will pick it up automatically."""
    import pathlib as _pathlib

    with get_conn() as conn:
        project_row = get_project_or_404(conn, project_id)
        check_project_active(conn, project_id)

        now = utcnow()
        intent_id = next_intent_id(conn, project_id)
        description = body.description.strip() or f"探索目标站点 {body.target_url}，产出站点地图、API 清单和页面-API 映射"

        project_worker_pool = project_row["worker_pool"] if "worker_pool" in project_row.keys() else "[]"
        conn.execute(
            "INSERT INTO intents (id, project_id, description, creator, worker_pool, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, description, "explore-ui", project_worker_pool or "[]", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("explore_target_url", project_id, body.target_url),
        )

        parent_id = project_row["parent_project_id"] or "unknown"
        output_dir = f"/tmp/axiom-files/{parent_id}/{project_id}/ai"
        _pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)

        return ExploreStatus(
            project_id=project_id,
            intent_id=intent_id,
            status="running",
            target_url=body.target_url,
            output_dir=output_dir,
            created_at=now,
        )


@router.get("/projects/{project_id}/explore", response_model=ExploreStatus)
def get_explore_status(project_id: str):
    """Get the current explore status and output file existence for a project."""
    import pathlib as _pathlib

    with get_conn() as conn:
        project_row = get_project_or_404(conn, project_id)
        parent_id = project_row["parent_project_id"] or "unknown"
        output_dir = f"/tmp/axiom-files/{parent_id}/{project_id}/ai"

        intent_row = conn.execute(
            "SELECT id, worker, concluded_at FROM intents "
            "WHERE project_id = ? AND creator = 'explore-ui' "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()

        fact_row = conn.execute(
            "SELECT description FROM facts WHERE project_id = ? AND id = 'explore_target_url'",
            (project_id,),
        ).fetchone()
        target_url = fact_row["description"] if fact_row else ""

        if intent_row is None:
            status = "idle"
            intent_id = None
            intent_worker = None
        elif intent_row["concluded_at"]:
            status = "done"
            intent_id = intent_row["id"]
            intent_worker = intent_row["worker"]
        else:
            status = "running"
            intent_id = intent_row["id"]
            intent_worker = intent_row["worker"]

        base = _pathlib.Path(output_dir)
        return ExploreStatus(
            project_id=project_id,
            intent_id=intent_id,
            intent_worker=intent_worker,
            status=status,
            target_url=target_url,
            output_dir=output_dir,
            sitemap_exists=(base / "sitemap.yaml").exists(),
            api_list_exists=(base / "api-list.yaml").exists(),
            api_page_map_exists=(base / "api-page-map.yaml").exists(),
        )
