from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from axiom.server import db
from axiom.server.routers import crawler


def _configure_test_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "axiom.db")


def _base_meta(tmp_path: Path) -> tuple[dict, Path]:
    output_dir = tmp_path / "crawler"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "crawler.log"
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("flush prompt {intent_id} {ai_dir} {intent_description}", encoding="utf-8")
    return {
        "project_id": "pg_test",
        "output_dir": str(output_dir),
        "log": str(log_path),
        "target_url": "https://example.com/home",
        "session_id": "crawler-pg_test",
        "agent_dir": str(tmp_path / "agent"),
        "session_dir": str(tmp_path / "sessions"),
        "active_window_seconds": 180,
        "flush_window_seconds": 45,
        "status": "running",
        "phase": "idle",
        "phase_started_at": None,
        "phase_pid": None,
        "last_flush_at": None,
    }, prompt_path


def _write_minimal_schema(output_dir: Path) -> None:
    output_dir.joinpath("site_map.yaml").write_text(
        "version: 1\n"
        "target:\n"
        "  url: https://example.com/home\n"
        "  domain: example.com\n"
        "  started_at: '2026-06-18T00:00:00Z'\n"
        "pages:\n"
        "  - id: page_home\n"
        "    name: Home\n"
        "    url: https://example.com/home\n",
        encoding="utf-8",
    )
    output_dir.joinpath("api_inventory.yaml").write_text(
        "version: 1\n"
        "target:\n"
        "  url: https://example.com/home\n"
        "  domain: example.com\n"
        "  started_at: '2026-06-18T00:00:00Z'\n"
        "apis: []\n",
        encoding="utf-8",
    )
    output_dir.joinpath("page_api_map.yaml").write_text(
        "version: 1\n"
        "target:\n"
        "  url: https://example.com/home\n"
        "  domain: example.com\n"
        "  started_at: '2026-06-18T00:00:00Z'\n"
        "mappings: []\n",
        encoding="utf-8",
    )


def _increment_payload_json() -> str:
    return (
        '{"accepted": true, "data": {"description": "increment ok", "pages": ['
        '{"id":"page_home","name":"Home","url":"https://example.com/home","path":"/home","page_type":"landing"}'
        '], "apis": [], "mappings": []}}\n'
    )


def test_flush_phase_rejects_stale_yaml_when_worker_never_reports_success(tmp_path, monkeypatch) -> None:
    _configure_test_db(tmp_path, monkeypatch)
    meta, prompt_path = _base_meta(tmp_path)
    output_dir = Path(meta["output_dir"])
    _write_minimal_schema(output_dir)
    monkeypatch.setattr(crawler, "CRAWLER_TRAFFIC_FILE", tmp_path / "empty-proxy.jsonl", raising=False)

    monkeypatch.setattr(crawler, "_build_pi_argv", lambda **_kwargs: ["fake-pi"])
    monkeypatch.setattr(crawler.subprocess, "run", lambda *args, **kwargs: None)

    class FakeProc:
        def __init__(self, _argv, stdout=None, **_kwargs) -> None:
            self.pid = 43210
            self.stdout = stdout

        def wait(self, timeout=None):  # noqa: ARG002
            if self.stdout:
                self.stdout.write("flush worker exited without final payload\n")
                self.stdout.flush()
            return 0

    monkeypatch.setattr(crawler.subprocess, "Popen", FakeProc)

    assert crawler._run_phase_once(
        cfg={"pi_model": "test-model"},
        meta=meta,
        prompt_path=prompt_path,
        phase="flush",
    ) == "failed"

    assert meta["last_flush_at"] is None
    with db.get_conn() as conn:
        page_count = conn.execute("SELECT COUNT(*) FROM crawler_pages").fetchone()[0]
    assert page_count == 0


def test_flush_phase_persists_schema_after_worker_reports_success(tmp_path, monkeypatch) -> None:
    _configure_test_db(tmp_path, monkeypatch)
    meta, prompt_path = _base_meta(tmp_path)
    output_dir = Path(meta["output_dir"])
    _write_minimal_schema(output_dir)
    monkeypatch.setattr(crawler, "CRAWLER_TRAFFIC_FILE", tmp_path / "empty-proxy.jsonl", raising=False)

    monkeypatch.setattr(crawler, "_build_pi_argv", lambda **_kwargs: ["fake-pi"])
    monkeypatch.setattr(crawler.subprocess, "run", lambda *args, **kwargs: None)

    class FakeProc:
        def __init__(self, _argv, stdout=None, **_kwargs) -> None:
            self.pid = 54321
            self.stdout = stdout

        def wait(self, timeout=None):  # noqa: ARG002
            if self.stdout:
                self.stdout.write(_increment_payload_json())
                self.stdout.flush()
            return 0

    monkeypatch.setattr(crawler.subprocess, "Popen", FakeProc)

    crawler._run_phase_once(
        cfg={"pi_model": "test-model"},
        meta=meta,
        prompt_path=prompt_path,
        phase="flush",
    )

    assert meta["last_flush_at"] is not None
    with db.get_conn() as conn:
        page_count = conn.execute("SELECT COUNT(*) FROM crawler_pages").fetchone()[0]
    assert page_count == 1


def test_crawler_status_does_not_treat_historical_success_as_current_done(tmp_path, monkeypatch) -> None:
    _configure_test_db(tmp_path, monkeypatch)
    log_path = tmp_path / "crawler.log"
    log_path.write_text(
        '\n=== phase flush start 2026-06-18T00:00:00Z ===\n'
        '{"accepted": true, "data": {"description": "old flush ok"}}\n'
        '\n=== phase final_flush start 2026-06-18T01:00:00Z ===\n'
        'final flush crashed before summary\n',
        encoding="utf-8",
    )
    meta = {
        "project_id": "pg_test",
        "status": "running",
        "phase": "final_flush",
        "pid": 111,
        "phase_pid": 222,
        "output_dir": str(tmp_path / "crawler"),
        "log": str(log_path),
        "phase_accepted": False,
    }

    monkeypatch.setattr(crawler, "_read_meta", lambda _project_id: meta)
    monkeypatch.setattr(crawler, "_is_running", lambda _pid: False)
    monkeypatch.setattr(crawler, "_write_meta", lambda _project_id, data: meta.update(data))

    status = crawler.get_crawler_status("pg_test")

    assert status.status == "failed"


def test_flush_phase_can_fallback_to_recent_traffic_when_worker_never_summarizes(tmp_path, monkeypatch) -> None:
    _configure_test_db(tmp_path, monkeypatch)
    meta, prompt_path = _base_meta(tmp_path)
    output_dir = Path(meta["output_dir"])
    _write_minimal_schema(output_dir)

    traffic_path = tmp_path / "proxy_flows.jsonl"
    traffic_path.write_text(
        '{"id":"flow_1","ts":1781767210.17,"method":"POST","url":"https://desk.redhaze.top/api/desk/tickets",'
        '"request_headers":{"content-type":"application/json","origin":"https://desk.redhaze.top","referer":"https://desk.redhaze.top/desk/tickets/new"},'
        '"request_body":"{\\"subject\\":\\"Test ticket\\"}","status_code":401,'
        '"response_headers":{"content-type":"application/json"},'
        '"response_body":"{\\"message\\":\\"login required\\"}\\n","content_type":"application/json"}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(crawler, "_build_pi_argv", lambda **_kwargs: ["fake-pi"])
    monkeypatch.setattr(crawler.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(crawler, "CRAWLER_TRAFFIC_FILE", traffic_path, raising=False)

    class FakeProc:
        def __init__(self, _argv, stdout=None, **_kwargs) -> None:
            self.pid = 65432
            self.stdout = stdout

        def wait(self, timeout=None):  # noqa: ARG002
            if self.stdout:
                self.stdout.write("flush worker exited without final payload\n")
                self.stdout.flush()
            return 0

    monkeypatch.setattr(crawler.subprocess, "Popen", FakeProc)

    crawler._run_phase_once(
        cfg={"pi_model": "test-model"},
        meta=meta,
        prompt_path=prompt_path,
        phase="flush",
    )

    assert meta["last_flush_at"] is not None
    with db.get_conn() as conn:
        page_rows = conn.execute("SELECT page_id, data_json FROM crawler_pages ORDER BY page_id").fetchall()
        api_rows = conn.execute("SELECT api_id, data_json FROM crawler_apis ORDER BY api_id").fetchall()
        map_rows = conn.execute("SELECT map_id, data_json FROM crawler_page_maps ORDER BY map_id").fetchall()
    assert len(page_rows) == 1
    assert len(api_rows) == 1
    assert len(map_rows) == 1
    assert any("desk/tickets" in row["data_json"] for row in api_rows)


def test_flush_phase_merges_traffic_with_dedup_on_success(tmp_path, monkeypatch) -> None:
    """Flush with a valid increment merges traffic — dedup prevents duplicates."""
    _configure_test_db(tmp_path, monkeypatch)
    meta, prompt_path = _base_meta(tmp_path)
    output_dir = Path(meta["output_dir"])
    _write_minimal_schema(output_dir)

    traffic_path = tmp_path / "proxy_flows.jsonl"
    traffic_path.write_text(
        '{"id":"flow_1","ts":1781767210.17,"method":"POST","url":"https://desk.redhaze.top/api/desk/tickets",'
        '"request_headers":{"content-type":"application/json","origin":"https://desk.redhaze.top","referer":"https://desk.redhaze.top/desk/tickets/new"},'
        '"request_body":"{\\"subject\\":\\"Test ticket\\"}","status_code":401,'
        '"response_headers":{"content-type":"application/json"},'
        '"response_body":"{\\"message\\":\\"login required\\"}\\n","content_type":"application/json"}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(crawler, "_build_pi_argv", lambda **_kwargs: ["fake-pi"])
    monkeypatch.setattr(crawler.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(crawler, "CRAWLER_TRAFFIC_FILE", traffic_path, raising=False)

    class FakeProc:
        def __init__(self, _argv, stdout=None, **_kwargs) -> None:
            self.pid = 76543
            self.stdout = stdout

        def wait(self, timeout=None):  # noqa: ARG002
            if self.stdout:
                self.stdout.write(_increment_payload_json())
                self.stdout.flush()
            return 0

    monkeypatch.setattr(crawler.subprocess, "Popen", FakeProc)

    crawler._run_phase_once(
        cfg={"pi_model": "test-model"},
        meta=meta,
        prompt_path=prompt_path,
        phase="flush",
    )

    with db.get_conn() as conn:
        page_rows = conn.execute("SELECT page_id FROM crawler_pages ORDER BY page_id").fetchall()
        api_rows = conn.execute("SELECT api_id, data_json FROM crawler_apis ORDER BY api_id").fetchall()
        map_rows = conn.execute("SELECT map_id FROM crawler_page_maps ORDER BY map_id").fetchall()
    # page_home from increment + desk page from traffic (different URLs, no dedup)
    assert len(page_rows) == 2
    # desk API from traffic
    assert len(api_rows) == 1
    assert any("desk/tickets" in row["data_json"] for row in api_rows)
    assert len(map_rows) == 1


def test_crawl_phase_merges_traffic_with_dedup_on_success(tmp_path, monkeypatch) -> None:
    """Crawl with a valid increment merges traffic — dedup prevents duplicates."""
    _configure_test_db(tmp_path, monkeypatch)
    meta, prompt_path = _base_meta(tmp_path)
    output_dir = Path(meta["output_dir"])
    _write_minimal_schema(output_dir)
    meta["started_at"] = "2026-06-18T00:00:00Z"

    traffic_path = tmp_path / "proxy_flows.jsonl"
    traffic_path.write_text(
        '{"id":"flow_1","ts":1781767210.17,"method":"POST","url":"https://desk.redhaze.top/api/desk/tickets",'
        '"request_headers":{"content-type":"application/json","origin":"https://desk.redhaze.top","referer":"https://desk.redhaze.top/desk/tickets/new"},'
        '"request_body":"{\\"subject\\":\\"Test ticket\\"}","status_code":401,'
        '"response_headers":{"content-type":"application/json"},'
        '"response_body":"{\\"message\\":\\"login required\\"}\\n","content_type":"application/json"}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(crawler, "_build_pi_argv", lambda **_kwargs: ["fake-pi"])
    monkeypatch.setattr(crawler.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(crawler, "CRAWLER_TRAFFIC_FILE", traffic_path, raising=False)

    class FakeProc:
        def __init__(self, _argv, stdout=None, **_kwargs) -> None:
            self.pid = 87654
            self.stdout = stdout

        def wait(self, timeout=None):  # noqa: ARG002
            if self.stdout:
                self.stdout.write(_increment_payload_json())
                self.stdout.flush()
            return 0

    monkeypatch.setattr(crawler.subprocess, "Popen", FakeProc)

    crawler._run_phase_once(
        cfg={"pi_model": "test-model"},
        meta=meta,
        prompt_path=prompt_path,
        phase="crawl",
    )

    with db.get_conn() as conn:
        page_rows = conn.execute("SELECT page_id FROM crawler_pages ORDER BY page_id").fetchall()
        api_rows = conn.execute("SELECT api_id FROM crawler_apis ORDER BY api_id").fetchall()
        map_rows = conn.execute("SELECT map_id FROM crawler_page_maps ORDER BY map_id").fetchall()
    # page_home from increment + desk page from traffic (different URLs)
    assert len(page_rows) == 2
    assert len(api_rows) == 1
    assert len(map_rows) == 1
