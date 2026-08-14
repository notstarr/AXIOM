from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
import pytest


SKILLS = Path(__file__).parents[1] / "src" / "axiom" / "dispatcher" / "skills"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_xss_detector_uses_put_for_modern_chrome_new_tab(monkeypatch) -> None:
    module = _load_script("xss_detect", SKILLS / "xss-verify" / "scripts" / "detect.py")
    seen: list[urllib.request.Request] = []

    class Response:
        @staticmethod
        def read() -> bytes:
            return b'{"webSocketDebuggerUrl":"ws://tab"}'

    def fake_urlopen(request, timeout):
        seen.append(request)
        assert timeout == 5
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    assert module.create_new_tab(9223, "data:text/html,<script>alert(1)</script>") == "ws://tab"
    assert seen[0].get_method() == "PUT"
    assert "%3Cscript%3E" in seen[0].full_url


def test_xss_detector_attaches_before_navigating(monkeypatch) -> None:
    module = _load_script("xss_detect_navigation", SKILLS / "xss-verify" / "scripts" / "detect.py")
    created_urls: list[str] = []
    closed_tabs: list[str] = []

    monkeypatch.setattr(
        module,
        "create_new_tab",
        lambda _port, url: created_urls.append(url) or "ws://tab",
    )
    monkeypatch.setattr(
        module,
        "get_tabs",
        lambda _port: [{"id": "tab-1", "webSocketDebuggerUrl": "ws://tab"}],
    )
    monkeypatch.setattr(
        module,
        "close_tab",
        lambda _port, tab_id: closed_tabs.append(tab_id),
    )

    class FakeWebSocketApp:
        def __init__(self, _url, *, on_open, on_message, on_error):
            self.on_open = on_open
            self.on_message = on_message
            self.on_error = on_error
            self.sent: list[dict] = []

        def send(self, payload: str) -> None:
            message = json.loads(payload)
            self.sent.append(message)
            if message.get("method") == "Page.navigate":
                self.on_message(
                    self,
                    json.dumps(
                        {
                            "method": "Page.javascriptDialogOpening",
                            "params": {"message": "smoke", "type": "alert"},
                        }
                    ),
                )

        def run_forever(self) -> None:
            self.on_open(self)

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(module.websocket, "WebSocketApp", FakeWebSocketApp)
    monkeypatch.setattr(module.sys, "argv", ["detect.py", "https://target.example/xss"])

    with pytest.raises(SystemExit) as exited:
        module.main()

    assert exited.value.code == 0
    assert created_urls == ["about:blank"]
    assert closed_tabs == ["tab-1"]




def test_browser_control_skill_is_a_native_tool_guide_only() -> None:
    skill_dir = SKILLS / "browser-control"
    assert (skill_dir / "SKILL.md").is_file()
    assert not (skill_dir / "scripts").exists()
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "Browser Broker" in content
    assert "browser_open_page" in content
    assert "browser_read_network" in content


def test_miniapp_analysis_generates_reqable_traffic_workflow(tmp_path) -> None:
    script = SKILLS / "miniapp" / "scripts" / "miniapp_decompile_analyze.py"
    source = tmp_path / "decompiled"
    source.mkdir()
    (source / "app.js").write_text(
        'wx.request({url:"https://api.example.test/client/app/order?code=order/detail"})',
        encoding="utf-8",
    )
    out_dir = tmp_path / "analysis"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "analyze",
            "--input",
            str(source),
            "--out",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    workflow_path = Path(result["traffic_workflow"])
    assert workflow_path == out_dir / "traffic_workflow.md"
    workflow = workflow_path.read_text(encoding="utf-8")
    assert "Reqable" in workflow
    assert "capture_live_filter" in workflow
    assert "capture_live_set_enabled" in workflow
    assert "capture_live_get_by_id" in workflow
    assert "capture_live_generate_curl" in workflow
    assert "get_current_database_context" not in workflow
    assert "query_http_flow" not in workflow
    assert "http_fuzzer" not in workflow
    assert "traffic_workflow.md" in (out_dir / "summary.md").read_text(encoding="utf-8")

    compatibility = (out_dir / "yak_workflow.md").read_text(encoding="utf-8")
    assert "traffic_workflow.md" in compatibility
    assert "Reqable" in compatibility


def test_miniapp_skill_uses_reqable_workflow() -> None:
    content = (SKILLS / "miniapp" / "SKILL.md").read_text(encoding="utf-8")

    assert "traffic_workflow.md" in content
    assert "capture_live_filter" in content
    assert "capture_live_get_by_id" in content
    assert "capture_live_generate_curl" in content
    assert "Yak MCP" not in content
    assert "query_http_flow" not in content
    assert "http_fuzzer" not in content
