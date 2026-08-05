#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from axiom.shared.database import AXIOM_SCHEMA, create_database_engine


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def health(api_url: str) -> dict:
    with urllib.request.urlopen(api_url.rstrip("/") + "/health", timeout=10) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description="Observe AXIOM PostgreSQL after production cutover")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:8888")
    parser.add_argument("--duration-seconds", type=int, default=86400)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    samples_path = args.output_dir / "samples.jsonl"
    summary_path = args.output_dir / "summary.json"
    engine = create_database_engine(
        args.database_url,
        application_name="axiom-post-cutover-observer",
        pool_size=1,
        max_overflow=0,
    )
    started = time.monotonic()
    samples = 0
    failures: list[dict] = []
    maximums = {"connections": 0, "waiting_locks": 0, "active_leases": 0}
    try:
        while time.monotonic() - started < args.duration_seconds:
            sample: dict[str, object] = {"observed_at": now()}
            try:
                sample["health"] = health(args.api_url)
                with engine.connect() as conn:
                    database_stats = conn.execute(
                        text(
                            "SELECT numbackends, deadlocks, conflicts, temp_files "
                            "FROM pg_stat_database WHERE datname = current_database()"
                        )
                    ).mappings().one()
                    waiting_locks = int(
                        conn.execute(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE database = (SELECT oid FROM pg_database "
                                "WHERE datname = current_database()) AND NOT granted"
                            )
                        ).scalar_one()
                    )
                    active_leases = int(
                        conn.execute(
                            text(
                                f"SELECT "
                                f"(SELECT count(*) FROM \"{AXIOM_SCHEMA}\".intents "
                                " WHERE status IN ('claimed','running','concluding')) + "
                                f"(SELECT count(*) FROM \"{AXIOM_SCHEMA}\".reason_runs "
                                " WHERE status IN ('claimed','running','concluding')) + "
                                f"(SELECT count(*) FROM \"{AXIOM_SCHEMA}\".vulnerability_review_runs "
                                " WHERE status IN ('claimed','running','concluding'))"
                            )
                        ).scalar_one()
                    )
                    duplicate_reason = int(
                        conn.execute(
                            text(
                                f"SELECT count(*) FROM (SELECT task_id FROM \"{AXIOM_SCHEMA}\".reason_runs "
                                "WHERE status IN ('pending','claimed','running','concluding') "
                                "GROUP BY task_id HAVING count(*) > 1) AS duplicated"
                            )
                        ).scalar_one()
                    )
                    duplicate_review = int(
                        conn.execute(
                            text(
                                f"SELECT count(*) FROM (SELECT task_id, vulnerability_id "
                                f"FROM \"{AXIOM_SCHEMA}\".vulnerability_review_runs "
                                "WHERE status IN ('pending','claimed','running','concluding') "
                                "GROUP BY task_id, vulnerability_id HAVING count(*) > 1) AS duplicated"
                            )
                        ).scalar_one()
                    )
                sample["database"] = {
                    **dict(database_stats),
                    "waiting_locks": waiting_locks,
                    "active_leases": active_leases,
                    "duplicate_active_reason_tasks": duplicate_reason,
                    "duplicate_active_reviews": duplicate_review,
                }
                maximums["connections"] = max(maximums["connections"], int(database_stats["numbackends"]))
                maximums["waiting_locks"] = max(maximums["waiting_locks"], waiting_locks)
                maximums["active_leases"] = max(maximums["active_leases"], active_leases)
                if duplicate_reason or duplicate_review:
                    failures.append({"observed_at": sample["observed_at"], "error": "duplicate active work", "sample": sample["database"]})
            except Exception as exc:  # observation must continue after one failed sample
                sample["error"] = f"{type(exc).__name__}: {exc}"
                failures.append({"observed_at": sample["observed_at"], "error": sample["error"]})
            with samples_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
            samples += 1
            time.sleep(args.interval_seconds)
    finally:
        engine.dispose()
    summary = {
        "started_at": datetime.fromtimestamp(time.time() - (time.monotonic() - started), timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "samples": samples,
        "maximums": maximums,
        "failures": failures,
        "passed": not failures,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
