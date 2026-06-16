from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from axiom.server.db import current_db_path

LOG = logging.getLogger(__name__)


def record_tool_executions_from_session(
    *,
    session_path: str,
    project_id: str,
    project_group_id: str | None = None,
) -> tuple[int, dict[str, int]]:
    """Parse a pi session JSONL file and insert tool execution records.

    Returns (records_inserted, token_usage) where token_usage has keys
    input_tokens, output_tokens, total_tokens summed across all assistant turns.
    """
    path = Path(session_path)
    if not path.exists():
        return 0, {}

    records, token_usage = _parse_session(path)
    if not records and not token_usage:
        return 0, {}

    db_path = current_db_path()
    if not db_path.exists():
        return 0, token_usage

    inserted = 0
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        for record in records:
            conn.execute(
                """
                INSERT OR IGNORE INTO tool_executions (
                    id, tool, kind, argv_json, status,
                    returncode, stdout, stderr, duration_ms,
                    project_group_id, project_id, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["tool"],
                    record["kind"],
                    record["argv_json"],
                    record["status"],
                    record["returncode"],
                    record["stdout"],
                    record["stderr"],
                    record["duration_ms"],
                    project_group_id or "",
                    project_id,
                    record["created_at"],
                    record["finished_at"],
                ),
            )
            inserted += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        LOG.exception("failed to write tool_executions")
    finally:
        conn.close()

    if inserted:
        LOG.info(
            "recorded %d tool executions project=%s session=%s",
            inserted,
            project_id,
            path.name,
        )
    return inserted, token_usage


def _parse_session(session_path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse a pi session JSONL file for toolCall/toolResult pairs and token usage.

    Returns (tool_records, token_usage_sum).
    """
    messages: list[dict[str, Any]] = []
    token_totals: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    try:
        with open(session_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "message":
                    msg = event.get("message") or {}
                    messages.append(msg)
                    # Accumulate token usage from assistant messages
                    if msg.get("role") == "assistant":
                        usage = msg.get("usage") or {}
                        if usage:
                            # pi format: input/output/totalTokens
                            inp = _safe_int(usage.get("input", usage.get("input_tokens", usage.get("inputTokens"))))
                            out = _safe_int(usage.get("output", usage.get("output_tokens", usage.get("outputTokens"))))
                            total = _safe_int(usage.get("totalTokens", usage.get("total_tokens")))
                            if inp or out or total:
                                token_totals["input_tokens"] += inp or 0
                                token_totals["output_tokens"] += out or 0
                                token_totals["total_tokens"] += total or (inp or 0) + (out or 0)
    except Exception:
        LOG.exception("failed to read session file path=%s", session_path)
        return [], {}

    # De-duplicate: keep only the final cumulative total (last assistant message wins)
    # but since pi counts per-turn, we keep the running sum
    token_usage = {k: v for k, v in token_totals.items() if v > 0}

    # Index tool results by toolCallId
    results: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") != "toolResult":
            continue
        call_id = msg.get("toolCallId")
        if not isinstance(call_id, str):
            continue
        output_text = ""
        for block in (msg.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                output_text = block.get("text", "")
                break
        results[call_id] = {
            "output": output_text,
            "is_error": bool(msg.get("isError", False)),
            "tool_name": msg.get("toolName", ""),
        }

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for block in (msg.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "toolCall":
                continue
            call_id = block.get("id", "")
            if not call_id or call_id in seen:
                continue
            seen.add(call_id)

            tool_name = block.get("name", "unknown")
            # pi session format: arguments or input
            inp = block.get("arguments") or block.get("input") or {}
            result = results.get(call_id, {})
            output = result.get("output", "")
            is_error = result.get("is_error", False)

            argv = _tool_input_to_argv(tool_name, inp)
            now = _utcnow()

            records.append({
                "id": call_id,
                "tool": tool_name,
                "kind": "builtin",
                "argv_json": json.dumps(argv, ensure_ascii=False, separators=(",", ":")),
                "status": "error" if is_error else "success",
                "returncode": 1 if is_error else 0,
                "stdout": _truncate(output) if not is_error else "",
                "stderr": _truncate(output) if is_error else "",
                "duration_ms": 0,
                "created_at": now,
                "finished_at": now,
            })

    return records, token_usage


def _safe_int(value: Any) -> int:
    """Convert value to non-negative int, return 0 on failure."""
    if value is None:
        return 0
    try:
        v = int(value)
        return v if v >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _tool_input_to_argv(tool_name: str, inp: dict[str, Any]) -> list[str]:
    argv = [tool_name]
    if tool_name == "bash":
        cmd = inp.get("command", "")
        if isinstance(cmd, str) and cmd.strip():
            argv.append(cmd.strip())
        timeout = inp.get("timeout")
        if timeout is not None:
            argv.append(f"(timeout={timeout})")
    elif tool_name in ("read", "edit"):
        path_val = inp.get("path") or inp.get("file_path") or ""
        if isinstance(path_val, str) and path_val.strip():
            argv.append(path_val.strip())
        if tool_name == "edit":
            old_str = inp.get("old_string", "")
            new_str = inp.get("new_string", "")
            if isinstance(old_str, str) and old_str:
                argv.append(f"(old={_truncate(old_str, 60)})")
            if isinstance(new_str, str) and new_str:
                argv.append(f"(new={_truncate(new_str, 60)})")
    elif tool_name == "write":
        path_val = inp.get("file_path") or inp.get("path") or ""
        if isinstance(path_val, str) and path_val.strip():
            argv.append(path_val.strip())
        content = inp.get("content", "")
        if isinstance(content, str):
            argv.append(f"(content={_truncate(content, 60)})")
    elif tool_name == "grep":
        pattern = inp.get("pattern", "")
        if isinstance(pattern, str):
            argv.append(pattern)
        path_val = inp.get("path") or inp.get("directory") or ""
        if isinstance(path_val, str):
            argv.append(path_val)
    elif tool_name in ("find", "ls"):
        path_val = inp.get("path") or inp.get("directory") or ""
        if isinstance(path_val, str) and path_val.strip():
            argv.append(path_val.strip())
    else:
        for k, v in inp.items():
            argv.append(f"{k}={_truncate(str(v), 100)}")
    return argv


def _truncate(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, total {len(text)} chars)"


def _utcnow() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
