"""Slot pool tests: per-lease context isolation, capacity, guardrails, self-heal."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from axiom.dispatcher.browser_broker import (
    BrowserBrokerServer,
    LeaseState,
    PageState,
)


class FakeChromium:
    def __init__(self, browser: "FakeBrowser") -> None:
        self._browser = browser

    async def connect_over_cdp(self, url: str, timeout: float | None = None) -> "FakeBrowser":
        self._browser.reconnects += 1
        return self._browser


class FakePlaywright:
    def __init__(self, browser: "FakeBrowser") -> None:
        self.chromium = FakeChromium(browser)


class FakeContext:
    def __init__(self, storage_state: str | None = None) -> None:
        self.storage_state = storage_state
        self.pages: list[object] = []
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = [FakeContext()]  # default context
        self.closed = False
        self.reconnects = 0

    def is_connected(self) -> bool:
        return not self.closed

    async def new_context(self, storage_state: str | None = None) -> FakeContext:
        context = FakeContext(storage_state=storage_state)
        self.contexts.append(context)
        return context


def _server(tmp_path: Path, *, slots: int = 4, page_limit: int = 8, idle: float = 600.0) -> tuple[BrowserBrokerServer, FakeBrowser]:
    server = BrowserBrokerServer(
        manager_secret="secret",
        socket_path=tmp_path / "broker.sock",
        ready=threading.Event(),
    )
    browser = FakeBrowser()
    server._playwright = FakePlaywright(browser)  # type: ignore[assignment]
    server._slot_limit = slots
    server._page_limit = page_limit
    server._slot_idle_seconds = idle
    return server, browser


def _payload(lease_id: str, run_dir: Path, **extra: object) -> dict[str, object]:
    return {
        "op": "_register_lease",
        "token": f"tok-{lease_id}",
        "lease_id": lease_id,
        "task_id": "t_001",
        "intent_id": "i001",
        "run_dir": str(run_dir / lease_id),
        "scope": {},
        "cdp_url": "http://127.0.0.1:9223",
        "wait_seconds": 0.2,
        **extra,
    }


def test_slot_pool_gives_each_lease_its_own_context(tmp_path: Path) -> None:
    server, browser = _server(tmp_path)
    asyncio.run(server._register_lease(_payload("l1", tmp_path)))
    asyncio.run(server._register_lease(_payload("l2", tmp_path)))
    lease1 = server.leases["tok-l1"]
    lease2 = server.leases["tok-l2"]
    assert lease1.slot is not None and lease2.slot is not None
    assert lease1.slot.context is not lease2.slot.context
    # 默认上下文（persistent 专用）不被占用
    assert lease1.slot.context is not browser.contexts[0]
    assert not lease1.slot.persistent


def test_slot_pool_injects_session_snapshot(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    asyncio.run(
        server._register_lease(_payload("l1", tmp_path, session_path=str(session_file)))
    )
    slot = server.leases["tok-l1"].slot
    assert slot is not None
    assert slot.context.storage_state == str(session_file)


def test_slot_pool_rejects_missing_session_snapshot(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    with pytest.raises(Exception) as raised:
        asyncio.run(
            server._register_lease(
                _payload("l1", tmp_path, session_path=str(tmp_path / "nope.json"))
            )
        )
    assert raised.value.code == "browser_session_missing"  # type: ignore[attr-defined]
    assert "tok-l1" not in server.leases


def test_slot_pool_exhaustion_parks_then_times_out(tmp_path: Path) -> None:
    server, _ = _server(tmp_path, slots=1)
    asyncio.run(server._register_lease(_payload("l1", tmp_path)))
    with pytest.raises(Exception) as raised:
        asyncio.run(server._register_lease(_payload("l2", tmp_path)))
    assert raised.value.code == "slot_pool_exhausted"  # type: ignore[attr-defined]


def test_slot_pool_frees_capacity_on_lease_close(tmp_path: Path) -> None:
    server, _browser = _server(tmp_path, slots=1)
    asyncio.run(server._register_lease(_payload("l1", tmp_path)))
    first_slot = server.leases["tok-l1"].slot
    assert first_slot is not None

    async def scenario() -> None:
        task = asyncio.create_task(server._register_lease(_payload("l2", tmp_path)))
        await asyncio.sleep(0.1)
        await server._close_lease("tok-l1")
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    slot = server.leases["tok-l2"].slot
    assert slot is not None
    assert slot.context is not first_slot.context
    assert first_slot.context.closed is True


def test_slot_page_limit_blocks_extra_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _ = _server(tmp_path, page_limit=2)
    asyncio.run(server._register_lease(_payload("l1", tmp_path)))
    lease = server.leases["tok-l1"]
    assert lease.slot is not None
    for index in range(2):
        lease.pages[f"p{index:03d}"] = PageState(
            page_id=f"p{index:03d}", page=object(), target_id=f"t{index}"
        )
    monkeypatch.setattr(server, "_require_target", lambda lease, url: None)
    with pytest.raises(Exception) as raised:
        asyncio.run(server._open_page(lease, {"url": "https://x.test"}))
    assert raised.value.code == "slot_page_limit"  # type: ignore[attr-defined]


def test_persistent_slot_is_exclusive(tmp_path: Path) -> None:
    server, _ = _server(tmp_path)
    asyncio.run(server._register_lease(_payload("l1", tmp_path, persistent=True)))
    with pytest.raises(Exception) as raised:
        asyncio.run(
            server._register_lease(_payload("l2", tmp_path, persistent=True))
        )
    assert raised.value.code == "slot_pool_exhausted"  # type: ignore[attr-defined]
    # 非 persistent 租约与 persistent 租约互不挤占
    asyncio.run(server._register_lease(_payload("l3", tmp_path)))
    assert server.leases["tok-l3"].slot is not None
    assert server.leases["tok-l1"].slot is not None
    assert server.leases["tok-l1"].slot.persistent is True


def test_idle_slots_release_capacity(tmp_path: Path) -> None:
    server, _browser = _server(tmp_path, slots=1, idle=5.0)
    asyncio.run(server._register_lease(_payload("l1", tmp_path)))
    first_slot = server.leases["tok-l1"].slot
    assert first_slot is not None
    # 回溯闲置时间戳：无页面且超过闲置阈值 → 新注册触发回收并成功
    first_slot.last_active -= 60.0
    asyncio.run(server._register_lease(_payload("l2", tmp_path)))
    assert server.leases["tok-l1"].slot is None
    assert first_slot.context.closed is True
    assert server.leases["tok-l2"].slot is not None


def test_browser_restart_recreates_slot_contexts(tmp_path: Path) -> None:
    server, browser = _server(tmp_path)
    asyncio.run(server._register_lease(_payload("l1", tmp_path)))
    lease = server.leases["tok-l1"]
    assert lease.slot is not None
    old_context = lease.slot.context
    lease.pages["p001"] = PageState(page_id="p001", page=object(), target_id="t1")

    # 浏览器进程崩溃后重连：注册时的首次连接 + 本次恢复重连
    browser.closed = True
    asyncio.run(server._ensure_browser(lease))
    assert lease.slot is not None
    assert lease.slot.context is not old_context
    assert lease.pages == {}
    assert browser.reconnects == 2


