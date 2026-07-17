#!/usr/bin/env python3
"""Backfill proj_073 maintainer/stepfun-flash token usage events.

The maintainer task previously never emitted a "token usage" event, so the
diagnostics token board for proj_073 had no stepfun-flash worker and the
maintainer phase always showed 0 tokens. This script reconstructs those events
from the pi session JSONL files and attributes them to proj_073 via the
tool_executions table (unique tool-call ids).

Dry-run by default. Pass --execute to actually INSERT into task_events.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

SESSION_DIR = Path("/tmp/axiom-pi/stepfun-flash/sessions")
DB_PATH = Path.home() / ".local/share/cairn/cairn.db"
PROJECT_ID = "proj_073"
WORKER = "stepfun-flash"
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-(\d{3})Z")


def _safe(value) -> int:
    if value is None:
        return 0
    try:
        v = int(value)
        return v if v >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _created_at_from_name(name: str) -> str:
    ts_raw = name.split("_")[0]
    m = TS_RE.match(ts_raw)
    if not m:
        return ""
    return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z"


def parse_session(path: Path) -> tuple[set[str], dict[str, int]]:
    token_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    tool_ids: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "message":
                    continue
                msg = ev.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage") or {}
                if usage:
                    inp = _safe(usage.get("input", usage.get("input_tokens", usage.get("inputTokens"))))
                    out = _safe(usage.get("output", usage.get("output_tokens", usage.get("outputTokens"))))
                    cr = _safe(usage.get("cacheRead", usage.get("cache_read", usage.get("cached_tokens"))))
                    cw = _safe(usage.get("cacheWrite", usage.get("cache_write")))
                    rs = _safe(usage.get("reasoning", usage.get("reasoningTokens", usage.get("reasoning_tokens"))))
                    tot = _safe(usage.get("totalTokens", usage.get("total_tokens")))
                    if inp or out or cr or cw or rs or tot:
                        token_totals["input_tokens"] += inp
                        token_totals["output_tokens"] += out
                        token_totals["cache_read_tokens"] += cr
                        token_totals["cache_write_tokens"] += cw
                        token_totals["reasoning_tokens"] += rs
                        token_totals["total_tokens"] += tot or (inp + out + cr + cw)
                for block in (msg.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        cid = block.get("id", "")
                        if cid:
                            tool_ids.add(cid)
    except Exception as e:
        print(f"  [warn] failed to read {path.name}: {e}")
    return tool_ids, {k: v for k, v in token_totals.items() if v > 0}


def main() -> int:
    execute = "--execute" in sys.argv
    if not SESSION_DIR.exists():
        print(f"session dir missing: {SESSION_DIR}")
        return 2
    if not DB_PATH.exists():
        print(f"db missing: {DB_PATH}")
        return 2

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT count(*) AS c FROM task_events WHERE project_id=? AND task_type='maintainer' AND message='token usage'",
            (PROJECT_ID,),
        ).fetchone()["c"]
        if execute and existing > 0:
            print(f"ABORT: {existing} maintainer token events already exist for {PROJECT_ID}; refusing to double-insert. Use a fresh DB or delete them first.")
            return 3

        # Load all proj_073 tool-call ids for reverse-lookup attribution.
        proj_ids = {
            r["id"]
            for r in conn.execute(
                "SELECT DISTINCT id FROM tool_executions WHERE project_id=?", (PROJECT_ID,)
            ).fetchall()
        }
    finally:
        conn.close()

    would_emit = 0
    skip_no_usage = 0
    skip_no_tool_ids = 0
    skip_not_proj = 0
    total_tokens = 0
    plans: list[tuple[str, dict[str, int], str]] = []

    for f in sorted(SESSION_DIR.glob("*.jsonl")):
        tool_ids, usage = parse_session(f)
        if not usage:
            skip_no_usage += 1
            continue
        if not tool_ids:
            skip_no_tool_ids += 1
            continue
        if not (tool_ids & proj_ids):
            skip_not_proj += 1
            continue
        created_at = _created_at_from_name(f.name)
        if not created_at:
            skip_not_proj += 1
            continue
        would_emit += 1
        total_tokens += usage.get("total_tokens", 0)
        plans.append((created_at, usage, f.name))

    print(f"would emit: {would_emit}")
    print(f"skip no_usage: {skip_no_usage} | no tool ids: {skip_no_tool_ids} | not proj_073: {skip_not_proj}")
    print(f"total_tokens to add: {total_tokens}")

    if not execute:
        print("\n[DRY-RUN] no changes made. Re-run with --execute to write.")
        return 0

    # Execute the INSERTs.
    conn = sqlite3.connect(str(DB_PATH))
    try:
        inserted = 0
        for created_at, usage, fname in plans:
            payload = json.dumps({"token_usage": usage}, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO task_events
                    (project_id, task_type, phase, worker, level, message, payload_json, created_at)
                VALUES (?, 'maintainer', 'maintainer_execute', ?, 'info', 'token usage', ?, ?)
                """,
                (PROJECT_ID, WORKER, payload, created_at),
            )
            inserted += 1
        conn.commit()
        print(f"\n[EXECUTED] inserted {inserted} token usage events into task_events.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
