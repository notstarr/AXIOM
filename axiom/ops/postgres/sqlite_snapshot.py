#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import apsw

MINIMUM_SQLITE = (3, 50, 7)


def version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split(".")[:3])  # type: ignore[return-value]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scalar(connection: apsw.Connection, sql: str):
    row = connection.execute(sql).fetchone()
    return row[0] if row else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and validate an AXIOM SQLite backup")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--checkpoint",
        action="store_true",
        help="Run a TRUNCATE WAL checkpoint first; only use after all writers stop.",
    )
    args = parser.parse_args()
    if version_tuple(apsw.sqlitelibversion()) < MINIMUM_SQLITE:
        raise SystemExit(
            f"APSW SQLite {apsw.sqlitelibversion()} is unsafe; need >=3.50.7"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_flags = apsw.SQLITE_OPEN_READWRITE if args.checkpoint else apsw.SQLITE_OPEN_READONLY
    source = apsw.Connection(str(args.source.resolve()), flags=source_flags)
    try:
        if args.checkpoint:
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        integrity = str(scalar(source, "PRAGMA integrity_check"))
        foreign_keys = source.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise SystemExit(
                f"SQLite validation failed: integrity={integrity}, fk={len(foreign_keys)}"
            )
        args.output.unlink(missing_ok=True)
        target = apsw.Connection(
            str(args.output),
            flags=apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE,
        )
        try:
            with target.backup("main", source, "main") as backup:
                while not backup.done:
                    backup.step(1_000)
        finally:
            target.close()
    finally:
        source.close()

    verified = apsw.Connection(str(args.output), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        integrity = str(scalar(verified, "PRAGMA integrity_check"))
        foreign_keys = verified.execute("PRAGMA foreign_key_check").fetchall()
        revision_row = verified.execute("SELECT version_num FROM alembic_version").fetchone()
        counts = {
            table: scalar(verified, f'SELECT count(*) FROM "{table}"')
            for table in (
                "tasks",
                "intents",
                "facts",
                "artifacts",
                "vulnerabilities",
                "reason_runs",
                "vulnerability_review_runs",
                "llm_token_usage",
            )
        }
    finally:
        verified.close()
    if integrity != "ok" or foreign_keys:
        raise SystemExit(f"SQLite backup failed validation: integrity={integrity}, fk={len(foreign_keys)}")
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": str(args.source.resolve()),
        "snapshot": str(args.output.resolve()),
        "apsw_version": apsw.apswversion(),
        "sqlite_version": apsw.sqlitelibversion(),
        "revision": revision_row[0] if revision_row else "",
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "size": args.output.stat().st_size,
        "sha256": sha256(args.output),
        "counts": counts,
    }
    args.manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(args.output, 0o600)
    os.chmod(args.manifest, 0o600)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
