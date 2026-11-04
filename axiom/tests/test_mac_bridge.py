from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from axiom.app import create_app
from axiom.modules.mac_bridge.schemas import DeviceInfo
from axiom.modules.mac_bridge.service import MacBridgeService


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------


def test_device_token_created_with_0600(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    token = bridge.device_token()
    assert token.startswith("mb_")

    path = data_dir / "mac_bridge_token"
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    # 第二次调用必须复用同一个 token，否则设备端要重新配一遍
    assert bridge.device_token() == token


def test_device_token_env_overrides_file(data_dir: Path, monkeypatch):
    monkeypatch.setenv("AXIOM_MAC_BRIDGE_TOKEN", "mb_from_env")
    bridge = MacBridgeService(data_dir)
    assert bridge.device_token() == "mb_from_env"
    assert bridge.verify_device_token("mb_from_env")
    assert not bridge.verify_device_token("mb_wrong")


def test_device_token_verify_rejects_empty(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    assert not bridge.verify_device_token("")
    assert not bridge.verify_device_token("mb_" + "x" * 64)


# ---------------------------------------------------------------------------
# 服务层：不需要 WS、不需要数据库
# ---------------------------------------------------------------------------


class _FakeDevice:
    """一个假设备：记录发出去的帧，允许测试手动回帧。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = 0

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed += 1


def _run(coro):
    return asyncio.run(coro)


def test_call_without_device_returns_error(data_dir: Path):
    bridge = MacBridgeService(data_dir)

    async def go():
        return await bridge.call(op="exec", args={"command": "uname"})

    resp = _run(go())
    assert resp.ok is False
    assert resp.code == "E_NO_DEVICE"


def test_call_with_multiple_online_devices_is_explicitly_ambiguous(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake1, fake2 = _FakeDevice(), _FakeDevice()

    async def go():
        await bridge.attach(
            device_id="dev-1",
            info=DeviceInfo(device_id="dev-1"),
            send=fake1.send,
            close=fake1.close,
        )
        await bridge.attach(
            device_id="dev-2",
            info=DeviceInfo(device_id="dev-2"),
            send=fake2.send,
            close=fake2.close,
        )
        return await bridge.call(op="exec", args={"command": "uname"})

    resp = _run(go())
    assert resp.ok is False
    assert resp.code == "E_MULTIPLE_DEVICES"
    assert fake1.sent == []
    assert fake2.sent == []


def test_call_exec_roundtrip(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake = _FakeDevice()

    async def go():
        await bridge.attach(
            device_id="dev-1",
            info=DeviceInfo(device_id="dev-1", hostname="mac", os="darwin", arch="arm64"),
            send=fake.send,
            close=fake.close,
        )
        task = asyncio.create_task(
            bridge.call(op="exec", args={"command": "uname -a"}, timeout_ms=5_000)
        )
        # 等请求真的发出去
        for _ in range(50):
            if fake.sent:
                break
            await asyncio.sleep(0.01)
        assert fake.sent, "网关没有把请求发给设备"
        req = fake.sent[0]
        assert req["type"] == "exec"
        assert req["payload"]["command"] == "uname -a"

        await bridge.on_frame(
            bridge._devices["dev-1"],
            json.dumps({
                "v": 1,
                "id": req["id"],
                "type": "result",
                "payload": {"ok": True, "stdout": "Darwin mac 24.6.0", "stderr": "", "exit_code": 0},
            }),
        )
        return await task

    resp = _run(go())
    assert resp.ok is True
    assert resp.result["stdout"] == "Darwin mac 24.6.0"
    assert resp.result["exit_code"] == 0


def test_nonzero_exit_and_truncation_are_normal_result_fields(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake = _FakeDevice()

    async def go():
        await bridge.attach(
            device_id="dev-1",
            info=DeviceInfo(device_id="dev-1"),
            send=fake.send,
            close=fake.close,
        )
        task = asyncio.create_task(
            bridge.call(op="exec", args={"command": "printf 'ok\\n'; exit 7"}, timeout_ms=5_000)
        )
        for _ in range(50):
            if fake.sent:
                break
            await asyncio.sleep(0.01)
        await bridge.on_frame(
            bridge._devices["dev-1"],
            json.dumps({
                "v": 1,
                "id": fake.sent[0]["id"],
                "type": "result",
                "payload": {
                    "ok": True,
                    "stdout": "ok\n",
                    "stderr": "",
                    "exit_code": 7,
                    "duration_ms": 12,
                    "truncated_bytes": 4096,
                },
            }),
        )
        return await task

    resp = _run(go())
    assert resp.ok is True
    assert resp.device_id == "dev-1"
    assert resp.result["stdout"] == "ok\n"
    assert resp.result["exit_code"] == 7
    assert resp.result["duration_ms"] == 12
    assert resp.result["truncated_bytes"] == 4096


def test_device_error_is_translated(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake = _FakeDevice()

    async def go():
        await bridge.attach(
            device_id="dev-1",
            info=DeviceInfo(device_id="dev-1"),
            send=fake.send,
            close=fake.close,
        )
        task = asyncio.create_task(bridge.call(op="exec", args={"command": "cat ~/.ssh/x"}, timeout_ms=5_000))
        for _ in range(50):
            if fake.sent:
                break
            await asyncio.sleep(0.01)
        await bridge.on_frame(
            bridge._devices["dev-1"],
            json.dumps({"v": 1, "id": fake.sent[0]["id"], "type": "error",
                        "payload": {"code": "E_COMMAND_DENIED", "message": "command rejected"}}),
        )
        return await task

    resp = _run(go())
    assert resp.ok is False
    assert resp.code == "E_COMMAND_DENIED"


def test_operator_off_keeps_state_but_refuses_calls(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake = _FakeDevice()

    async def go():
        conn = await bridge.attach(
            device_id="dev-1",
            info=DeviceInfo(device_id="dev-1", hostname="mac"),
            send=fake.send,
            close=fake.close,
        )
        await bridge.on_frame(
            conn, json.dumps({"v": 1, "type": "bye", "reason": "operator_off"})
        )
        # 状态必须留着，UI 要能解释"是操作员关的"
        devices = bridge.list_devices()
        assert len(devices) == 1
        assert devices[0].state == "operator_off"

        # 但不能再接请求
        return await bridge.call(op="exec", args={"command": "id"})

    resp = _run(go())
    assert resp.ok is False
    assert resp.code == "E_NO_DEVICE"


def test_explicit_disabled_device_reports_disabled(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake = _FakeDevice()

    async def go():
        conn = await bridge.attach(
            device_id="dev-1",
            info=DeviceInfo(device_id="dev-1"),
            send=fake.send,
            close=fake.close,
        )
        await bridge.on_frame(
            conn, json.dumps({"v": 1, "type": "bye", "reason": "operator_off"})
        )
        return await bridge.call(
            op="exec",
            args={"command": "id"},
            device_id="dev-1",
        )

    resp = _run(go())
    assert resp.ok is False
    assert resp.code == "E_DISABLED"


def test_reconnect_kicks_old_connection(data_dir: Path):
    bridge = MacBridgeService(data_dir)
    fake1, fake2 = _FakeDevice(), _FakeDevice()

    async def go():
        await bridge.attach(device_id="dev-1", info=DeviceInfo(device_id="dev-1"),
                            send=fake1.send, close=fake1.close)
        await bridge.attach(device_id="dev-1", info=DeviceInfo(device_id="dev-1"),
                            send=fake2.send, close=fake2.close)
        assert fake1.closed == 1, "旧连接必须被踢掉"

        # 新连接可用
        task = asyncio.create_task(bridge.call(op="exec", args={"command": "id"}, timeout_ms=5_000))
        for _ in range(50):
            if fake2.sent:
                break
            await asyncio.sleep(0.01)
        await bridge.on_frame(
            bridge._devices["dev-1"],
            json.dumps({"v": 1, "id": fake2.sent[0]["id"], "type": "result",
                        "payload": {"ok": True, "stdout": "uid=501", "exit_code": 0}}),
        )
        return await task

    resp = _run(go())
    assert resp.ok is True
    assert resp.result["stdout"] == "uid=501"


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc_client(engine, data_dir, monkeypatch):
    """带 service_token 的 app 客户端。"""
    token = "mb_test_service_token"
    (data_dir).mkdir(parents=True, exist_ok=True)
    (data_dir / "service_token").write_text(token + "\n")
    monkeypatch.setattr("axiom.modules.mac_bridge.token.load_or_create", lambda d: "mb_device_token")
    from fastapi.testclient import TestClient

    with TestClient(create_app(engine=engine, data_dir=data_dir)) as c:
        c.service_token = token  # type: ignore[attr-defined]
        yield c


def test_devices_endpoint_lists_registry(svc_client):
    r = svc_client.get("/api/v1/mac-bridge/devices")
    assert r.status_code == 200
    assert r.json() == {"devices": []}


def test_internal_call_requires_service_token(svc_client):
    # 不带凭据
    r = svc_client.post("/api/v1/internal/mac-bridge/call", json={"op": "exec", "args": {"command": "id"}})
    assert r.status_code == 403

    # 带错的
    r = svc_client.post(
        "/api/v1/internal/mac-bridge/call",
        json={"op": "exec", "args": {"command": "id"}},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 403

    # 带对的（此时没有设备，应返回 E_NO_DEVICE 而不是鉴权失败）
    r = svc_client.post(
        "/api/v1/internal/mac-bridge/call",
        json={"op": "exec", "args": {"command": "id"}},
        headers={"Authorization": f"Bearer {svc_client.service_token}"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "E_NO_DEVICE"


def test_ws_rejects_bad_token(svc_client):
    with pytest.raises(Exception):
        with svc_client.websocket_connect("/api/v1/mac-bridge/device?token=wrong"):
            pass
