from __future__ import annotations

import gzip
import json
import os
import time

from sqlalchemy import text


def _write_fresh_state(data_dir) -> None:
    """Pre-seed a just-rotated marker so overview() skips the lazy auto rotation."""
    storage_dir = data_dir / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (storage_dir / "state.json").write_text(
        json.dumps({"last_rotate_at": stamp, "actor": "test-fresh"}),
        encoding="utf-8",
    )


def test_storage_overview_measures_categories_and_database(client, data_dir):
    _write_fresh_state(data_dir)
    run_dir = data_dir / "runs" / "t_x"
    run_dir.mkdir(parents=True)
    (run_dir / "a.bin").write_bytes(b"x" * 1000)
    nested = run_dir / "sub"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"y" * 500)

    response = client.get("/api/v1/storage/overview")
    assert response.status_code == 200, response.text
    body = response.json()

    categories = {item["key"]: item for item in body["categories"]}
    assert categories["runs"]["bytes"] == 1500
    assert categories["runs"]["items"] == 2
    assert body["total_bytes"] >= body["categories"][0]["bytes"]
    assert body["disk"]["total_bytes"] > 0
    assert body["logs"]["keep_days"] == 30
    assert any(
        table["name"] == "llm_token_usage" for table in body["database"]["tables"]
    )
    assert body["database"]["total_bytes"] > 0


def test_overview_lazy_autorotates_stale_log(client, data_dir):
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    payload = b"stale-log-line\n" * 100
    (logs / "server.log").write_bytes(payload)

    assert not list(logs.glob("*.gz"))
    response = client.get("/api/v1/storage/overview")
    assert response.status_code == 200, response.text

    archives = list(logs.glob("server-*.log.gz"))
    assert len(archives) == 1
    assert gzip.decompress(archives[0].read_bytes()) == payload
    assert (logs / "server.log").stat().st_size == 0

    state = json.loads((data_dir / "storage" / "state.json").read_text())
    assert state["actor"] == "system.storage-auto"


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

    # 第二次轮转：生成新归档，内容为空（已截断），幂等不报错。
    second = client.post("/api/v1/storage/logs/rotate?actor=hexin-test")
    assert second.status_code == 200, second.text
    assert len(list(logs.glob("server-*.log.gz"))) == 2
    second_result = second.json()
    assert second_result["rotated"][0]["original_bytes"] == 0


def test_rotate_purges_expired_archives_but_keeps_recent(client, data_dir):
    logs = data_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "server.log").write_bytes(b"data")
    expired = logs / "server-20200101-000000.log.gz"
    expired_bytes_payload = gzip.compress(b"expired-content")
    expired.write_bytes(expired_bytes_payload)
    past = time.time() - 45 * 86_400
    os.utime(expired, (past, past))
    recent = logs / "server-20990101-000000.log.gz"
    recent.write_bytes(gzip.compress(b"recent"))

    response = client.post("/api/v1/storage/logs/rotate?actor=hexin-test")
    assert response.status_code == 200, response.text
    result = response.json()

    assert "server-20200101-000000.log.gz" in result["purged_archives"]
    assert result["purged_archives_bytes"] == len(expired_bytes_payload)
    assert not expired.exists()
    assert recent.exists()
