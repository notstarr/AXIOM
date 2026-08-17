from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from axiom.dispatcher.browser_broker import (
    BROWSER_TOOL_NAMES,
    BrowserBrokerError,
    BrowserBrokerManager,
    BrowserBrokerServer,
    LeaseState,
    browser_skill_directory,
    PageState,
    RefState,
    redact_headers,
    target_allowed,
)


def _browser_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "browser-control"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: browser-control\n---\n", encoding="utf-8")
    return skill


def test_browser_broker_registers_and_closes_lease_without_starting_chrome(
    tmp_path: Path,
) -> None:
    manager = BrowserBrokerManager(socket_dir=tmp_path)
    try:
        handle = manager.register_lease(
            lease_id="lease-1",
            task_id="t_001",
            intent_id="i001",
            run_dir=str(tmp_path / "run"),
            scope={"enabled": True, "targets": ["example.com"]},
            cdp_url="http://127.0.0.1:65530",
        )

        response = manager._request(
            {"op": "browser_list_pages", "token": handle.token, "params": {}}
        )
        assert response == {
            "ok": True,
            "content": "当前Browser Lease没有页面",
            "data": {"pages": []},
        }

        manager.close_lease(handle)
        closed = manager._request(
            {"op": "browser_list_pages", "token": handle.token, "params": {}}
        )
        assert closed["ok"] is False
        assert closed["error"]["code"] == "lease_not_found"
    finally:
        manager.close()


def test_browser_tool_contract_is_explicit_and_does_not_expose_session_identity() -> None:
    assert len(BROWSER_TOOL_NAMES) == 20
    assert "browser_open_page" in BROWSER_TOOL_NAMES
    assert "browser_snapshot" in BROWSER_TOOL_NAMES
    assert "browser_read_network" in BROWSER_TOOL_NAMES
    assert "browser_get_credentials" in BROWSER_TOOL_NAMES
    assert all("session" not in name and "lease" not in name for name in BROWSER_TOOL_NAMES)


def test_browser_skill_directory_only_requires_skill_markdown(tmp_path: Path) -> None:
    skill = _browser_skill(tmp_path)
    assert browser_skill_directory([str(skill)]) == skill


def _lease_for_chrome(tmp_path: Path) -> LeaseState:
    return LeaseState(
        token="token",
        lease_id="lease",
        task_id="t_001",
        intent_id="i001",
        run_dir=tmp_path / "run",
        scope={},
        cdp_url="http://127.0.0.1:19223",
    )


def test_browser_launcher_rejects_a_non_cdp_process_on_the_cdp_port(
    tmp_path: Path, monkeypatch
) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )
    monkeypatch.setattr(BrowserBrokerServer, "_cdp_alive", classmethod(lambda _cls, _url: False))
    monkeypatch.setattr(BrowserBrokerServer, "_port_is_open", staticmethod(lambda _host, _port: True))

    with pytest.raises(BrowserBrokerError) as raised:
        asyncio.run(server._launch_chrome(_lease_for_chrome(tmp_path)))

    assert raised.value.code == "browser_cdp_port_occupied"


def test_browser_launcher_reports_missing_chrome(tmp_path: Path, monkeypatch) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )
    monkeypatch.setattr(BrowserBrokerServer, "_cdp_alive", classmethod(lambda _cls, _url: False))
    monkeypatch.setattr(BrowserBrokerServer, "_port_is_open", staticmethod(lambda _host, _port: False))
    monkeypatch.setattr(BrowserBrokerServer, "_chrome_binary", staticmethod(lambda: None))

    with pytest.raises(BrowserBrokerError) as raised:
        asyncio.run(server._launch_chrome(_lease_for_chrome(tmp_path)))

    assert raised.value.code == "browser_launcher_missing"


def test_browser_launcher_uses_an_isolated_profile_and_expected_flags(
    tmp_path: Path, monkeypatch
) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )
    lease = _lease_for_chrome(tmp_path)
    monkeypatch.setenv("AXIOM_CHROME_PROFILE_DIR", str(tmp_path / "Chrome-SecTest"))
    monkeypatch.setattr(BrowserBrokerServer, "_cdp_alive", classmethod(lambda _cls, _url: next(alive)))
    monkeypatch.setattr(BrowserBrokerServer, "_port_is_open", staticmethod(lambda _host, _port: False))
    monkeypatch.setattr(BrowserBrokerServer, "_chrome_binary", staticmethod(lambda: "/fake/chrome"))
    monkeypatch.setattr(BrowserBrokerServer, "_find_expected_chrome_pid", classmethod(lambda _cls, **_kwargs: 4242))
    alive = iter((False, True))
    seen: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(server._launch_chrome(lease))

    args = list(seen["args"])
    assert args[0] == "/fake/chrome"
    assert f"--user-data-dir={tmp_path / 'Chrome-SecTest'}" in args
    assert "--remote-debugging-port=19223" in args
    assert "--remote-allow-origins=*" in args
    assert "--window-position=-32000,-32000" in args
    assert "--start-minimized" in args
    assert seen["kwargs"]["start_new_session"] is True
    assert server._chrome_pid == 4242
    if server._chrome_pid_file is not None:
        server._chrome_pid_file.unlink(missing_ok=True)


def test_browser_launcher_uses_headless_without_offscreen_window(
    tmp_path: Path, monkeypatch
) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )
    lease = _lease_for_chrome(tmp_path)
    monkeypatch.setenv("AXIOM_CHROME_PROFILE_DIR", str(tmp_path / "Chrome-SecTest"))
    monkeypatch.setenv("CHROME_HEADLESS", "1")
    monkeypatch.setattr(BrowserBrokerServer, "_cdp_alive", classmethod(lambda _cls, _url: next(alive)))
    monkeypatch.setattr(BrowserBrokerServer, "_port_is_open", staticmethod(lambda _host, _port: False))
    monkeypatch.setattr(BrowserBrokerServer, "_chrome_binary", staticmethod(lambda: "/fake/chrome"))
    monkeypatch.setattr(BrowserBrokerServer, "_find_expected_chrome_pid", classmethod(lambda _cls, **_kwargs: 4242))
    alive = iter((False, True))
    seen: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(server._launch_chrome(lease))

    args = list(seen["args"])
    assert "--headless" in args
    assert "--window-position=-32000,-32000" not in args
    if server._chrome_pid_file is not None:
        server._chrome_pid_file.unlink(missing_ok=True)


def test_browser_launcher_keeps_window_visible_when_chrome_window_normal(
    tmp_path: Path, monkeypatch
) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )
    lease = _lease_for_chrome(tmp_path)
    monkeypatch.setenv("AXIOM_CHROME_PROFILE_DIR", str(tmp_path / "Chrome-SecTest"))
    monkeypatch.setenv("CHROME_WINDOW", "normal")
    monkeypatch.setattr(BrowserBrokerServer, "_cdp_alive", classmethod(lambda _cls, _url: next(alive)))
    monkeypatch.setattr(BrowserBrokerServer, "_port_is_open", staticmethod(lambda _host, _port: False))
    monkeypatch.setattr(BrowserBrokerServer, "_chrome_binary", staticmethod(lambda: "/fake/chrome"))
    monkeypatch.setattr(BrowserBrokerServer, "_find_expected_chrome_pid", classmethod(lambda _cls, **_kwargs: 4242))
    alive = iter((False, True))
    seen: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(server._launch_chrome(lease))

    args = list(seen["args"])
    assert "--start-minimized" not in args
    assert "--window-position=-32000,-32000" not in args
    if server._chrome_pid_file is not None:
        server._chrome_pid_file.unlink(missing_ok=True)


class _FakeCdpSession:
    def __init__(self, window_state: str = "minimized") -> None:
        self.sent: list[tuple[str, object | None]] = []
        self.window_state = window_state

    async def send(self, method: str, params: object | None = None) -> dict[str, object]:
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        if method == "Browser.getWindowBounds":
            return {"bounds": {"windowState": self.window_state}}
        return {}

    async def detach(self) -> None:
        return None


class _FakeCdpPage:
    def __init__(self, window_state: str = "minimized") -> None:
        self.session = _FakeCdpSession(window_state=window_state)
        self.context = self

    async def new_cdp_session(self, page: "_FakeCdpPage") -> "_FakeCdpSession":
        return self.session


class _FakeCdpContext:
    def __init__(self, pages: list[_FakeCdpPage]) -> None:
        self.pages = pages


class _FakeCdpBrowser:
    def __init__(self, contexts: list[_FakeCdpContext]) -> None:
        self.contexts = contexts


def test_browser_broker_minimizes_all_windows_by_default(monkeypatch) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=Path("/tmp/broker-minimize.sock"),
        ready=threading.Event(),
    )
    monkeypatch.delenv("CHROME_WINDOW", raising=False)
    page = _FakeCdpPage()
    server._browser = _FakeCdpBrowser([_FakeCdpContext([page])])

    asyncio.run(server._minimize_all_windows())

    assert ("Browser.getWindowForTarget", None) in page.session.sent
    assert (
        "Browser.setWindowBounds",
        {"windowId": 7, "bounds": {"windowState": "minimized"}},
    ) in page.session.sent


def test_browser_broker_skips_minimizing_when_chrome_window_normal(monkeypatch) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=Path("/tmp/broker-normal.sock"),
        ready=threading.Event(),
    )
    monkeypatch.setenv("CHROME_WINDOW", "normal")
    page = _FakeCdpPage()
    server._browser = _FakeCdpBrowser([_FakeCdpContext([page])])

    asyncio.run(server._minimize_all_windows())

    assert page.session.sent == []


def test_browser_broker_attach_keeps_user_restored_window_visible(monkeypatch) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=Path("/tmp/broker-attach.sock"),
        ready=threading.Event(),
    )
    monkeypatch.delenv("CHROME_WINDOW", raising=False)
    page = _FakeCdpPage(window_state="normal")  # 用户手动恢复的窗口
    server._browser = _FakeCdpBrowser([_FakeCdpContext([page])])

    asyncio.run(server._minimize_only_if_already_minimized())

    # 只读窗口状态,绝不下发 setWindowBounds 压回用户手动恢复的窗口
    assert all(method != "Browser.setWindowBounds" for method, _ in page.session.sent)


def test_browser_broker_attach_reminimizes_when_all_windows_minimized(monkeypatch) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=Path("/tmp/broker-attach-min.sock"),
        ready=threading.Event(),
    )
    monkeypatch.delenv("CHROME_WINDOW", raising=False)
    page = _FakeCdpPage(window_state="minimized")
    server._browser = _FakeCdpBrowser([_FakeCdpContext([page])])

    asyncio.run(server._minimize_only_if_already_minimized())

    assert (
        "Browser.setWindowBounds",
        {"windowId": 7, "bounds": {"windowState": "minimized"}},
    ) in page.session.sent


class _FakeOpenContext:
    def __init__(self) -> None:
        self.opened: list[_FakeOpenPage] = []

    async def new_page(self) -> "_FakeOpenPage":
        page = _FakeOpenPage()
        self.opened.append(page)
        return page


class _FakeOpenPage:
    async def goto(self, *args, **kwargs) -> None:
        return None


class _FakeOpenState:
    def __init__(self) -> None:
        self.page_id = "pg_test"


def _open_page_server(monkeypatch, *, was_minimized: bool) -> tuple[BrowserBrokerServer, list[str]]:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=Path("/tmp/broker-open.sock"),
        ready=threading.Event(),
    )
    context = _FakeOpenContext()
    calls: list[str] = []

    async def fake_ensure_browser(_lease) -> _FakeOpenContext:
        return context

    async def fake_all_windows_minimized() -> bool:
        return was_minimized

    async def fake_minimize_all_windows() -> None:
        calls.append("minimize")

    async def fake_register_page(_lease, _page) -> _FakeOpenState:
        return _FakeOpenState()

    async def fake_page_response(_state, _message, *, extra=None) -> dict[str, object]:
        return {"ok": True}

    monkeypatch.setattr(server, "_ensure_browser", fake_ensure_browser)
    monkeypatch.setattr(server, "_all_windows_minimized", fake_all_windows_minimized)
    monkeypatch.setattr(server, "_minimize_all_windows", fake_minimize_all_windows)
    monkeypatch.setattr(server, "_register_page", fake_register_page)
    monkeypatch.setattr(server, "_page_response", fake_page_response)
    return server, calls


def test_browser_open_page_reminimizes_when_windows_were_minimized(
    tmp_path: Path, monkeypatch
) -> None:
    server, calls = _open_page_server(monkeypatch, was_minimized=True)

    result = asyncio.run(server._open_page(_lease_for_chrome(tmp_path), {"url": "https://x.test"}))

    assert result == {"ok": True}
    assert calls == ["minimize"]


def test_browser_open_page_keeps_user_restored_window_visible(
    tmp_path: Path, monkeypatch
) -> None:
    server, calls = _open_page_server(monkeypatch, was_minimized=False)

    result = asyncio.run(server._open_page(_lease_for_chrome(tmp_path), {"url": "https://x.test"}))

    assert result == {"ok": True}
    assert calls == []


def test_browser_get_credentials_filters_by_wildcard_and_subdomain(tmp_path: Path) -> None:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker-cred.sock",
        ready=threading.Event(),
    )
    lease = _lease_for_chrome(tmp_path)

    class FakeContext:
        async def cookies(self):
            return [
                {"name": "qq_root", "value": "1", "domain": ".qq.com", "httpOnly": True},
                {"name": "mail_sub", "value": "2", "domain": "mail.qq.com", "httpOnly": False},
                {"name": "other_site", "value": "3", "domain": "example.com", "httpOnly": False},
            ]

    async def fake_ensure_browser(_lease):
        return FakeContext()

    server._ensure_browser = fake_ensure_browser  # type: ignore[method-assign]

    # 1. 匹配 *.qq.com / qq.com (命中 qq_root 和 mail_sub)
    res_wildcard = asyncio.run(server._get_credentials(lease, {"domain": "*.qq.com"}))
    assert res_wildcard["ok"] is True
    data = res_wildcard["data"]
    assert data["cookie_count"] == 2
    assert "Cookie" in data["formatted_headers"]
    assert "qq_root=1" in data["formatted_headers"]["Cookie"]
    assert "mail_sub=2" in data["formatted_headers"]["Cookie"]

    # 2. 精确子域 mail.qq.com
    res_sub = asyncio.run(server._get_credentials(lease, {"domain": "mail.qq.com"}))
    assert res_sub["ok"] is True
    assert res_sub["data"]["cookie_count"] == 2  # 父域 .qq.com + 子域 mail.qq.com

    # 3. 不匹配域名
    res_none = asyncio.run(server._get_credentials(lease, {"domain": "notfound.org"}))
    assert res_none["ok"] is True
    assert res_none["data"]["cookie_count"] == 0


def test_browser_scope_reuses_task_target_rules() -> None:
    scope = {
        "enabled": True,
        "targets": ["https://example.com/app"],
        "exclude": ["blocked.example.com"],
    }

    assert target_allowed(scope, "https://example.com/dashboard")
    assert target_allowed(scope, "https://api.example.com/v1")
    assert not target_allowed(scope, "https://blocked.example.com/")
    assert not target_allowed(scope, "https://outside.test/")
    assert not target_allowed(scope, "file:///etc/passwd")


def test_browser_scope_supports_blacklist_only_and_ipv6_entries() -> None:
    blacklist_only = {
        "enabled": True,
        "targets": [],
        "exclude": ["blocked.example.com"],
    }
    assert target_allowed(blacklist_only, "https://api.allowed.example/v1")
    assert not target_allowed(blacklist_only, "https://api.blocked.example.com/v1")

    ipv6_scope = {
        "enabled": True,
        "targets": ["2001:db8::1"],
        "exclude": [],
    }
    assert target_allowed(ipv6_scope, "https://[2001:db8::1]/")
    assert not target_allowed(ipv6_scope, "https://[2001:db8::2]/")


def test_browser_network_headers_are_redacted_by_default() -> None:
    headers = {
        "Authorization": "Bearer secret",
        "Cookie": "sid=secret",
        "Content-Type": "application/json",
    }

    assert redact_headers(headers, include_sensitive=False) == {
        "Authorization": "[已脱敏]",
        "Cookie": "[已脱敏]",
        "Content-Type": "application/json",
    }
    assert redact_headers(headers, include_sensitive=True)["Authorization"] == "Bearer secret"


def test_browser_extension_registers_every_native_tool() -> None:
    extension = (
        Path(__file__).parents[1]
        / "src"
        / "axiom"
        / "dispatcher"
        / "extensions"
        / "axiom-browser"
        / "index.ts"
    ).read_text(encoding="utf-8")

    for name in BROWSER_TOOL_NAMES:
        assert f'name: "{name}"' in extension
    assert "AXIOM_BROWSER_SOCKET" in extension
    assert "AXIOM_BROWSER_TOKEN" in extension
    assert "session_id" not in extension
    assert 'pi.on("tool_call"' in extension
    assert "LEGACY_BROWSER_COMMANDS" in extension
    assert "禁止通过 Bash 调用旧浏览器脚本" in extension


def test_browser_page_and_ref_identity_are_lease_scoped(tmp_path: Path) -> None:
    class FakePage:
        @staticmethod
        def is_closed() -> bool:
            return False

    page = PageState(page_id="p001", page=FakePage(), target_id="target-1")  # type: ignore[arg-type]
    first = LeaseState(
        token="token-1",
        lease_id="lease-1",
        task_id="t_001",
        intent_id="i001",
        run_dir=tmp_path / "first",
        scope={},
        cdp_url="http://127.0.0.1:9223",
        pages={"p001": page},
    )
    second = LeaseState(
        token="token-2",
        lease_id="lease-2",
        task_id="t_001",
        intent_id="i002",
        run_dir=tmp_path / "second",
        scope={},
        cdp_url="http://127.0.0.1:9223",
    )
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )

    assert server._page(first, {"page_id": "p001"}) is page
    with pytest.raises(BrowserBrokerError) as cross_lease:
        server._page(second, {"page_id": "p001"})
    assert cross_lease.value.code == "page_not_found"

    page.refs["e1"] = RefState(
        ref="e1",
        generation=1,
        handle=object(),  # type: ignore[arg-type]
        role="button",
        accessible_name="提交",
        text="提交",
        attributes={},
        allowed_actions=("click",),
        frame_id="main",
        dom_key="main:1",
    )
    page.generation = 2
    with pytest.raises(BrowserBrokerError) as stale:
        server._ref(page, "e1", action="click")
    assert stale.value.code == "stale_ref"


def test_browser_broker_cancels_an_operation_when_the_client_disconnects(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = (
            Path(tempfile.gettempdir())
            / f"axiom-browser-cancel-{uuid.uuid4().hex[:8]}.sock"
        )
        broker = BrowserBrokerServer(
            manager_secret="secret",
            socket_path=socket_path,
            ready=threading.Event(),
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_dispatch(_payload):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        broker._dispatch = slow_dispatch  # type: ignore[method-assign]
        server = await asyncio.start_unix_server(
            broker._handle_client,
            path=str(socket_path),
        )
        try:
            _reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(json.dumps({"op": "slow"}).encode("utf-8") + b"\n")
            await writer.drain()
            await asyncio.wait_for(started.wait(), timeout=1)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(cancelled.wait(), timeout=1)
        finally:
            server.close()
            await server.wait_closed()
            socket_path.unlink(missing_ok=True)

    asyncio.run(scenario())
