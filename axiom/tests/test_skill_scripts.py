from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
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


def test_browser_launch_script_detaches_chrome_from_worker_pipe(tmp_path) -> None:
    launch = SKILLS / "browser-control" / "scripts" / "launch-chrome.sh"
    stop = SKILLS / "browser-control" / "scripts" / "stop-chrome.sh"
    port = _free_port()
    fake_chrome = _fake_chrome(tmp_path)
    args_file = tmp_path / "chrome-args.json"
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "CDP_PORT": str(port),
        "CHROME_BIN": str(fake_chrome),
        "FAKE_CHROME_ARGS_FILE": str(args_file),
        "MITM_PROXY": "1",
    }
    pid_file = Path(f"/tmp/chrome-sectest-{port}.pid")
    pid = 0

    try:
        started = time.monotonic()
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'bash "{launch}" 2>&1 | head -30',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert time.monotonic() - started < 5
        assert "Chrome-SecTest ready" in result.stdout
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        assert os.getpgid(pid) == pid
        args = json.loads(args_file.read_text(encoding="utf-8"))
        assert f"--remote-debugging-port={port}" in args
        assert "--proxy-server=http://127.0.0.1:18080" in args
        assert "--ignore-certificate-errors" in args

        reused = subprocess.run(
            ["/bin/bash", str(launch)],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
            env=environment,
        )
        assert reused.returncode == 0
        assert "already running" in reused.stdout
        assert int(pid_file.read_text(encoding="utf-8").strip()) == pid
    finally:
        subprocess.run(
            ["/bin/bash", str(stop)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        pid_file.unlink(missing_ok=True)


def test_browser_launch_script_refuses_foreign_cdp_process(tmp_path) -> None:
    launch = SKILLS / "browser-control" / "scripts" / "launch-chrome.sh"
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    environment = {**os.environ, "HOME": str(tmp_path), "CDP_PORT": str(port)}
    try:
        _wait_for_port(port)
        result = subprocess.run(
            ["/bin/bash", str(launch)],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
            env=environment,
        )

        assert result.returncode == 2
        assert "occupied by a non-SecTest Chrome instance" in result.stdout
        assert server.poll() is None
    finally:
        try:
            os.killpg(server.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        server.wait(timeout=3)
        Path(f"/tmp/chrome-sectest-{port}.pid").unlink(missing_ok=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"port {port} did not become ready")


def _fake_chrome(tmp_path: Path) -> Path:
    script = tmp_path / "fake-chrome.py"
    script.write_text(
        f"""#!{sys.executable}
import http.server
import json
import os
import sys

port = None
for argument in sys.argv[1:]:
    if argument.startswith("--remote-debugging-port="):
        port = int(argument.split("=", 1)[1])
if port is None:
    raise SystemExit(2)

args_file = os.environ.get("FAKE_CHROME_ARGS_FILE")
if args_file:
    with open(args_file, "w", encoding="utf-8") as handle:
        json.dump(sys.argv[1:], handle)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        payload = b'{{"Browser":"Fake Chrome"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return

http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script
