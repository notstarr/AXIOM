"""crawler/meta.py — process management, meta file I/O, and runtime settings."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any

from axiom.server.db import get_conn
from axiom.server.services import utcnow

from .constants import CRAWLER_SESSION_DIR


def pid_file(project_id: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/axiom-crawler-{project_id}.pid")


def meta_file(project_id: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/axiom-crawler-{project_id}.meta.json")


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        stat = pathlib.Path(f"/proc/{pid}/stat")
        if stat.exists():
            parts = stat.read_text().split()
            if len(parts) >= 3 and parts[2] == "Z":
                return False
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().startswith("Z"):
                return False
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_meta(project_id: str) -> dict:
    f = meta_file(project_id)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def write_meta(project_id: str, data: dict) -> None:
    meta_file(project_id).write_text(json.dumps(data, ensure_ascii=False))


def load_worker_config() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pi_model, pi_base_url, pi_api_key, pi_provider_api, pi_context_window "
            "FROM crawler_workers WHERE id = 1"
        ).fetchone()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(400, "爬虫 worker 尚未配置，请先在系统设置中配置")
    if not row["pi_model"] or not row["pi_base_url"]:
        from fastapi import HTTPException
        raise HTTPException(400, "爬虫 worker 配置不完整（缺少 pi_model 或 pi_base_url）")
    return dict(row)


def get_api_key_from_db_or_body(api_key_body: str) -> str:
    if api_key_body.strip():
        return api_key_body.strip()
    with get_conn() as conn:
        row = conn.execute("SELECT pi_api_key FROM crawler_workers WHERE id = 1").fetchone()
    return (row["pi_api_key"] or "") if row else ""


def load_crawler_runtime_settings() -> dict[str, int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT crawler_active_window_seconds, crawler_flush_window_seconds, crawler_total_timeout_seconds "
            "FROM settings WHERE rowid = 1"
        ).fetchone()
    return {
        "active_window_seconds": int(row["crawler_active_window_seconds"]),
        "flush_window_seconds": int(row["crawler_flush_window_seconds"]),
        "total_timeout_seconds": int(row["crawler_total_timeout_seconds"]),
    }


def phase_deadline_seconds(meta: dict) -> int:
    phase = str(meta.get("phase") or "idle")
    if phase in {"flush", "final_flush"}:
        return int(meta.get("flush_window_seconds") or 0)
    if phase == "crawl":
        return int(meta.get("active_window_seconds") or 0)
    return 0


def seconds_until_phase_switch(meta: dict) -> int | None:
    started_at = meta.get("phase_started_at")
    if not started_at:
        return None
    try:
        from datetime import datetime, timezone
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    import time
    budget = phase_deadline_seconds(meta)
    if budget <= 0:
        return None
    remaining = budget - int(time.time() - started.timestamp())
    return max(0, remaining)
