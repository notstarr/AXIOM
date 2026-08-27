from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

from sqlalchemy import text


def _wait_for_snapshot(data_dir, timeout_s: float = 15.0) -> dict:
    snapshot = data_dir / "storage" / "usage.json"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if snapshot.exists():
            return json.loads(snapshot.read_text(encoding="utf-8"))
        time.sleep(0.25)
    raise AssertionError(f"background snapshot not written within {timeout_s}s")


def _make_log(logs: Path, name: str, payload: bytes) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    (logs / name).write_bytes(payload)


def test_overview_first_call_returns_skeleton_then_background_fills(client, engine, data_dir):
    run_dir = data_dir / "runs" / "t_x"
    run_dir.mkdir(parents=True)
    (run_dir / "a.bin").write_bytes(b"x" * 1000)

    first = client.get("/api/v1/storage/overview")
    assert first.status_code == 200, first.text
    body = first.json()
    # 首次无快照：毫秒级骨架返回，绝不同步遍历大目录
    assert isinstance(body["categories"], list)
    assert body["database"]["total_bytes"] > 0

    snapshot = _wait_for_snapshot(data_dir)
    assert snapshot["total_bytes"] >= 1000
    assert any(cat["key"] == "runs" and cat["bytes"] == 1000 for cat in snapshot["categories"])

    second = client.get("/api/v1/storage/overview")
    filled = second.json()
    runs = next(item for item in filled["categories"] if item["key"] == "runs")
    assert runs["bytes"] == 1000
    assert runs["items"] == 1
    assert filled["refreshing"] is False
    assert any(t["name"] == "llm_token_usage" for t in filled["database"]["tables"])
    assert filled["logs"]["keep_days"] == 30


def test_refresh_endpoint_recomputes_and_caches(client, engine, data_dir):
    run_dir = data_dir / "runs" / "t_x"
    nested = run_dir / "sub"
    nested.mkdir(parents=True)
    (run_dir / "a.bin").write_bytes(b"x" * 1000)
    (nested / "b.bin").write_bytes(b"y" * 500)

    response = client.post("/api/v1/storage/refresh")
    assert response.status_code == 200, response.text
    body = response.json()

    runs = next(item for item in body["categories"] if item["key"] == "runs")
    assert runs["bytes"] == 1500
    assert runs["items"] == 2
    assert body["total_bytes"] >= 1500
    assert body["disk"]["total_bytes"] > 0
    assert any(t["name"] == "llm_token_usage" for t in body["database"]["tables"])

    cached = client.get("/api/v1/storage/overview")
    assert cached.json()["categories"] == body["categories"]


def test_refresh_performs_due_rotation_with_auto_actor(client, engine, data_dir):
    logs = data_dir / "logs"
    payload = b"stale-log-line\n" * 100
    _make_log(logs, "server.log", payload)

    response = client.post("/api/v1/storage/refresh")
    assert response.status_code == 200, response.text

    archives = list(logs.glob("server-*.log.gz"))
    assert len(archives) == 1
    assert gzip.decompress(archives[0].read_bytes()) == payload
    assert (logs / "server.log").stat().st_size == 0

    state = json.loads((data_dir / "storage" / "state.json").read_text())
    assert state["actor"] == "system.storage-auto"

    with engine.begin() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM axiom.audit_events "
                "WHERE event_type = 'storage.logs.rotated'"
            )
        ).scalar_one()
    assert count == 1


def test_log_rotate_truncates_archives_and_writes_audit(client, engine, data_dir):
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload = b"line-to-archive\n" * 40
    (logs / "server.log").write_bytes(payload)

    response = client.post("/api/v1/storage/logs/rotate?actor=hexin-test")
    assert response.status_code == 200, response.text
    result = response.json()

    assert len(result["rotated"]) == 1
    assert result["rotated"][0]["file"] == "server.log"
    assert result["rotated"][0]["original_bytes"] == len(payload)
    assert (logs / "server.log").stat().st_size == 0
    archives = list(logs.glob("server-*.log.gz"))
    assert len(archives) == 1
    assert archives[0].name == result["rotated"][0]["archived_to"]
    assert gzip.decompress(archives[0].read_bytes()) == payload

    with engine.begin() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM axiom.audit_events "
                "WHERE event_type = 'storage.logs.rotated'"
            )
        ).scalar_one()
    assert count == 1

    # 第二次轮转：生成新归档（不覆盖上一份），内容为空，幂等不报错。
    second = client.post("/api/v1/storage/logs/rotate?actor=hexin-test")
    assert second.status_code == 200, second.text
    archives_after = sorted(logs.glob("server-*.log.gz"))
    assert len(archives_after) == 2
    assert second.json()["rotated"][0]["original_bytes"] == 0


def test_rotate_purges_expired_archives_but_keeps_recent(client, data_dir):
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "server.log").write_bytes(b"data")
    expired = logs / "server-20200101-000000.log.gz"
    expired_payload = gzip.compress(b"expired-content")
    expired.write_bytes(expired_payload)
    past = time.time() - 45 * 86_400
    os.utime(expired, (past, past))
    recent = logs / "server-20990101-000000.log.gz"
    recent.write_bytes(gzip.compress(b"recent"))

    response = client.post("/api/v1/storage/logs/rotate?actor=hexin-test")
    assert response.status_code == 200, response.text
    result = response.json()

    assert "server-20200101-000000.log.gz" in result["purged_archives"]
    assert result["purged_archives_bytes"] == len(expired_payload)
    assert not expired.exists()
    assert recent.exists()
