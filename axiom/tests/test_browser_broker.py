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
    PageState,
    RefState,
    redact_headers,
    target_allowed,
)


def _browser_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "browser-control"
    script = skill / "scripts" / "launch-chrome.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o700)
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
            skill_directory=_browser_skill(tmp_path),
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
    assert len(BROWSER_TOOL_NAMES) == 19
    assert "browser_open_page" in BROWSER_TOOL_NAMES
    assert "browser_snapshot" in BROWSER_TOOL_NAMES
    assert "browser_read_network" in BROWSER_TOOL_NAMES
    assert all("session" not in name and "lease" not in name for name in BROWSER_TOOL_NAMES)


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
        launch_script=None,
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
        launch_script=None,
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
