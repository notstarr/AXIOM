#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

ACTIVE = ("claimed", "running", "concluding")


def snapshot(path: Path) -> dict[str, object]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        result: dict[str, object] = {}
        total = 0
        for label, table in (
            ("intents", "intents"),
            ("reason_runs", "reason_runs"),
            ("review_runs", "vulnerability_review_runs"),
        ):
            placeholders = ",".join("?" for _ in ACTIVE)
            rows = connection.execute(
                f"SELECT task_id, id, worker, lease_id, status, last_heartbeat_at "
                f"FROM {table} WHERE status IN ({placeholders}) ORDER BY task_id, id",
                ACTIVE,
            ).fetchall()
            result[label] = [dict(row) for row in rows]
            total += len(rows)
        result["total"] = total
        return result
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Wait until a frozen AXIOM SQLite DB has no leases")
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait_seconds
    while True:
        current = snapshot(args.sqlite_path)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(current, ensure_ascii=False))
        if current["total"] == 0:
            return
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
