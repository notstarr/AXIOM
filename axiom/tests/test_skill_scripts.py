from __future__ import annotations

import importlib.util
import json
import subprocess
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


def test_browser_launch_script_is_valid_for_macos_bash() -> None:
    script = SKILLS / "browser-control" / "scripts" / "launch-chrome.sh"
    result = subprocess.run(
        ["/bin/bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "EXTRA_FLAGS[@]" not in script.read_text(encoding="utf-8")
