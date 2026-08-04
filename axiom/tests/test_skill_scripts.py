from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path
from types import SimpleNamespace

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
    assert '[[ "${cmd}" != *"--type="* ]]' in script.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["launch-proxy.sh", "stop-proxy.sh"])
def test_browser_proxy_scripts_are_valid_for_macos_bash(name: str) -> None:
    script = SKILLS / "browser-control" / "scripts" / name
    result = subprocess.run(
        ["/bin/bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


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
        "MITM_AUTO_START": "0",
        "CHROME_HEADLESS": "1",
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
        assert "--headless" in args

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


def test_browser_stop_script_terminates_entire_chrome_process_group(tmp_path) -> None:
    launch = SKILLS / "browser-control" / "scripts" / "launch-chrome.sh"
    stop = SKILLS / "browser-control" / "scripts" / "stop-chrome.sh"
    port = _free_port()
    child_pid_file = tmp_path / "chrome-child.pid"
    fake_chrome = _fake_chrome(tmp_path, child_pid_file=child_pid_file)
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "CDP_PORT": str(port),
        "CHROME_BIN": str(fake_chrome),
        "CHROME_HEADLESS": "1",
    }
    pid_file = Path(f"/tmp/chrome-sectest-{port}.pid")
    parent_pid = 0
    child_pid = 0

    try:
        result = subprocess.run(
            ["/bin/bash", str(launch)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        parent_pid = int(pid_file.read_text(encoding="utf-8").strip())
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        assert os.getpgid(parent_pid) == parent_pid
        assert os.getpgid(child_pid) == parent_pid

        stopped = subprocess.run(
            ["/bin/bash", str(stop)],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
            env=environment,
        )

        assert stopped.returncode == 0, stopped.stderr or stopped.stdout
        _wait_for_process_exit(parent_pid)
        _wait_for_process_exit(child_pid)
    finally:
        if parent_pid:
            try:
                os.killpg(parent_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        pid_file.unlink(missing_ok=True)


def test_managed_proxy_does_not_inherit_worker_secrets(tmp_path) -> None:
    launch = SKILLS / "browser-control" / "scripts" / "launch-proxy.sh"
    stop = SKILLS / "browser-control" / "scripts" / "stop-proxy.sh"
    port = _free_port()
    fake_mitmdump = _fake_mitmdump(tmp_path)
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "MITM_PORT": str(port),
        "MITMDUMP_BIN": str(fake_mitmdump),
        "PI_API_KEY": "must-not-leak",
        "AXIOM_LEASE_ID": "lease-secret",
        "AXIOM_TASK_ID": "task-secret",
        "AXIOM_INTENT_ID": "intent-secret",
    }
    pid_file = Path(f"/tmp/axiom-mitmdump-{port}.pid")

    try:
        result = subprocess.run(
            ["/bin/bash", str(launch)],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
            env=environment,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        observed = json.loads(
            (tmp_path / "mitmdump-environment.json").read_text(encoding="utf-8")
        )
        assert observed == {
            "AXIOM_INTENT_ID": False,
            "AXIOM_LEASE_ID": False,
            "AXIOM_TASK_ID": False,
            "PI_API_KEY": False,
        }
    finally:
        subprocess.run(
            ["/bin/bash", str(stop)],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
            env=environment,
        )
        pid_file.unlink(missing_ok=True)


def test_cdp_tabs_are_registered_by_alias_and_closed_by_lease(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script(
        "browser_cdp_registry",
        SKILLS / "browser-control" / "scripts" / "cdp_ctl.py",
    )
    registry = tmp_path / "tabs.json"
    pages = [{"id": "target-1", "type": "page", "url": "https://target.example"}]
    closed: list[str] = []
    monkeypatch.setenv("AXIOM_CDP_TAB_REGISTRY", str(registry))
    monkeypatch.setenv("AXIOM_LEASE_ID", "lease-1")
    monkeypatch.setenv("AXIOM_INTENT_ID", "i001")
    monkeypatch.setattr(module, "_list_pages", lambda _cdp: pages)
    monkeypatch.setattr(module, "_close_tab", lambda _cdp, target_id: closed.append(target_id))

    module._register_tab("http://127.0.0.1:9223", "target-1", "i001")

    assert module._registered_target("http://127.0.0.1:9223", "i001") == "target-1"
    assert module._resolve_tab_info("http://127.0.0.1:9223", "i001")["id"] == "target-1"
    result = module.cmd_close_lease_tabs(
        SimpleNamespace(cdp="http://127.0.0.1:9223", lease_id="lease-1")
    )

    assert result == 0
    assert closed == ["target-1"]
    assert module._registered_target("http://127.0.0.1:9223", "i001") is None


def test_cdp_alias_never_replaces_another_active_lease(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script(
        "browser_cdp_lease_isolation",
        SKILLS / "browser-control" / "scripts" / "cdp_ctl.py",
    )
    registry = tmp_path / "tabs.json"
    monkeypatch.setenv("AXIOM_CDP_TAB_REGISTRY", str(registry))
    monkeypatch.setenv("AXIOM_LEASE_ID", "lease-other")
    monkeypatch.setenv("AXIOM_INTENT_ID", "i001")
    module._register_tab("http://127.0.0.1:9223", "target-other", "i001")
    monkeypatch.setenv("AXIOM_LEASE_ID", "lease-current")

    assert module._registered_target(
        "http://127.0.0.1:9223", "i001", current_lease_only=True
    ) is None


def test_cdp_refuses_implicit_page_selection_inside_lease(monkeypatch) -> None:
    module = _load_script(
        "browser_cdp_requires_explicit_tab",
        SKILLS / "browser-control" / "scripts" / "cdp_ctl.py",
    )
    monkeypatch.setenv("AXIOM_LEASE_ID", "lease-current")
    monkeypatch.setattr(
        module,
        "_list_pages",
        lambda _cdp: [{"id": "other", "type": "page", "url": "https://other.example"}],
    )

    with pytest.raises(module.CTLError, match="--tab-id is required"):
        module._resolve_tab_info("http://127.0.0.1:9223", None)


def test_cdp_select_uses_the_global_snapshot_index(monkeypatch) -> None:
    module = _load_script(
        "browser_cdp_select_index",
        SKILLS / "browser-control" / "scripts" / "cdp_ctl.py",
    )
    observed: dict[str, object] = {}

    class Choice:
        def as_element(self):
            return self

        def select_option(self, *, value, timeout):
            observed["value"] = value
            observed["timeout"] = timeout
            return [value]

        def dispose(self):
            observed["disposed"] = True

    class Page:
        def evaluate_handle(self, expression, index):
            observed["expression"] = expression
            observed["index"] = index
            return Choice()

    monkeypatch.setattr(
        module,
        "_get_visible_elements",
        lambda _page: [
            {"tag": "button"},
            {"tag": "input"},
            {"tag": "select"},
        ],
    )
    monkeypatch.setattr(module, "_post_action", lambda _page, _sleep: None)

    result = module.cmd_select(
        Page(),
        SimpleNamespace(target="2", value="chosen", sleep=0),
    )

    assert result == 0
    assert observed["index"] == 2
    assert "button" in str(observed["expression"])
    assert observed["value"] == "chosen"
    assert observed["disposed"] is True


def test_cdp_url_command_uses_http_metadata_without_playwright(monkeypatch) -> None:
    module = _load_script(
        "browser_cdp_fast_url",
        SKILLS / "browser-control" / "scripts" / "cdp_ctl.py",
    )
    monkeypatch.setattr(
        module,
        "_resolve_tab_info",
        lambda _cdp, _tab_id: {"id": "target-1", "url": "https://fast.example", "title": "Fast"},
    )
    monkeypatch.setattr(
        module,
        "_connect",
        lambda _cdp: (_ for _ in ()).throw(AssertionError("Playwright must not start")),
    )

    assert module.main(["url"]) == 0


def test_proxy_reader_returns_recent_matching_records_across_rotated_files(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_script(
        "proxy_reader",
        SKILLS / "browser-control" / "scripts" / "proxy_get_traffic.py",
    )
    monkeypatch.setenv("PROXY_FLOW_BACKUPS", "2")
    path = tmp_path / "flows.jsonl"
    older = [
        {"id": f"flow-{index}", "url": f"https://target/api/{index}", "content_type": "application/json"}
        for index in range(8)
    ]
    current = [
        {"id": f"flow-{index}", "url": f"https://target/api/{index}", "content_type": "application/json"}
        for index in range(8, 10)
    ]
    Path(f"{path}.1").write_text(
        "".join(json.dumps(item) + "\n" for item in older), encoding="utf-8"
    )
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in current), encoding="utf-8"
    )

    flows = module.load_flows(str(path), "/api/", "api", 4)

    assert [flow["id"] for flow in flows] == ["flow-6", "flow-7", "flow-8", "flow-9"]


def test_proxy_addon_rotates_bounded_jsonl(tmp_path: Path, monkeypatch) -> None:
    fake_mitmproxy = types.ModuleType("mitmproxy")
    fake_tls = types.ModuleType("mitmproxy.tls")
    fake_tls.ClientHelloData = object
    fake_mitmproxy.tls = fake_tls
    monkeypatch.setitem(sys.modules, "mitmproxy", fake_mitmproxy)
    monkeypatch.setitem(sys.modules, "mitmproxy.tls", fake_tls)
    flow_file = tmp_path / "flows.jsonl"
    monkeypatch.setenv("PROXY_FLOW_FILE", str(flow_file))
    module = _load_script(
        "proxy_addon_rotation",
        SKILLS / "browser-control" / "scripts" / "mitm_addon.py",
    )
    module.FLOW_MAX_BYTES = 120
    module.FLOW_BACKUPS = 2

    module._append_record({"id": "first", "body": "a" * 80})
    module._append_record({"id": "second", "body": "b" * 80})

    assert flow_file.exists()
    assert Path(f"{flow_file}.1").exists()
    assert json.loads(flow_file.read_text(encoding="utf-8"))["id"] == "second"
    assert json.loads(Path(f"{flow_file}.1").read_text(encoding="utf-8"))["id"] == "first"


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


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        if result.stdout.strip().startswith("Z"):
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} is still running")


def _fake_chrome(tmp_path: Path, *, child_pid_file: Path | None = None) -> Path:
    script = tmp_path / "fake-chrome.py"
    child_setup = ""
    if child_pid_file is not None:
        child_setup = f"""
import subprocess
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open({str(child_pid_file)!r}, "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
"""
    script.write_text(
        f"""#!{sys.executable}
import http.server
import json
import os
import sys
{child_setup}

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


def _fake_mitmdump(tmp_path: Path) -> Path:
    script = tmp_path / "fake-mitmdump.py"
    script.write_text(
        f"""#!{sys.executable}
import json
import os
import socket
import sys

port = int(sys.argv[sys.argv.index("--listen-port") + 1])
keys = ("AXIOM_INTENT_ID", "AXIOM_LEASE_ID", "AXIOM_TASK_ID", "PI_API_KEY")
with open(os.path.join(os.environ["HOME"], "mitmdump-environment.json"), "w", encoding="utf-8") as handle:
    json.dump({{key: key in os.environ for key in keys}}, handle, sort_keys=True)

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen()
while True:
    connection, _address = server.accept()
    connection.close()
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script
