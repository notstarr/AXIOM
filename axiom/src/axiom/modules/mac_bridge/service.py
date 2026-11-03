from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from axiom.modules.mac_bridge.schemas import CallResponse, DeviceInfo
from axiom.modules.mac_bridge import token as tokenmod

logger = logging.getLogger(__name__)

# ── 时序 ──────────────────────────────────────────────────────────────────
# 设备侧每 10s 发一次 ping。连续 3 次没消息就认为它不响应了。
#
# 为什么不能只看 socket 是否断开：macOS 合盖休眠时 TCP 连接仍然是
# ESTABLISHED，但进程不再跑代码。不判活的话每个请求都要各自等到自己的
# 超时，模型会以为"命令在慢慢跑"，实际上是永远不会有结果。
HEARTBEAT_SEC = 10
STALE_SEC = 30

# 一次调用的默认/上限超时（毫秒）。网关侧额外多等 10s，给设备的
# 进程组收尾留时间，超了才判定为超时。
DEFAULT_CALL_TIMEOUT_MS = 60_000
MAX_CALL_TIMEOUT_MS = 600_000


@dataclass
class _Pending:
    future: asyncio.Future
    created: float


@dataclass
class DeviceConn:
    device_id: str
    send: Callable[[dict[str, Any]], Awaitable[None]]
    close: Callable[[], Awaitable[None]]
    info: DeviceInfo
    # 状态只有 info.state 一份真相。曾经这里还有一个 state: str = "online"，
    # detach 写 info.state、_pick 读 conn.state，于是 operator_off 之后设备
    # 仍然会被挑中执行命令。删掉，不要重新引入第二份。
    last_seen: float = field(default_factory=time.monotonic)
    connected_at: float = field(default_factory=time.monotonic)
    pending: dict[str, _Pending] = field(default_factory=dict)


class MacBridgeService:
    """设备注册表 + 请求应答关联。线程安全靠单事件循环 + asyncio 原语。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._devices: dict[str, DeviceConn] = {}
        self._lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task | None = None

    # ── token ──────────────────────────────────────────────────────────
    def device_token(self) -> str:
        return tokenmod.load_or_create(self._data_dir)

    def verify_device_token(self, presented: str) -> bool:
        return tokenmod.verify(presented, self.device_token())

    # ── 连接生命周期 ────────────────────────────────────────────────────
    async def attach(
        self,
        *,
        device_id: str,
        info: DeviceInfo,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        close: Callable[[], Awaitable[None]],
    ) -> DeviceConn:
        async with self._lock:
            # 同一 device_id 的旧连接一律踢掉：手机/机器断网重连时旧 socket
            # 会半开着，留着会让请求发到一个不再读的连接上。
            old = self._devices.pop(device_id, None)
        if old is not None:
            logger.info("mac_bridge: kicking stale connection for %s", device_id)
            for p in list(old.pending.values()):
                if not p.future.done():
                    p.future.set_exception(RuntimeError("device reconnected"))
            with contextlib.suppress(Exception):
                await old.close()

        conn = DeviceConn(device_id=device_id, send=send, close=close, info=info)
        conn.info.state = "online"
        conn.info.connected_at = int(time.time())
        conn.info.last_seen = int(time.time())
        async with self._lock:
            self._devices[device_id] = conn
        self._ensure_watchdog()
        logger.info("mac_bridge: device online %s (%s)", device_id, info.hostname)
        return conn

    async def detach(self, conn: DeviceConn, *, reason: str = "disconnected") -> None:
        # 注意：**不**从注册表里删掉。设备关掉开关和网络掉线是两件不同的事，
        # UI 需要知道"是操作员关的"，而不是一律显示成网络故障。真要清理等
        # 下次 attach 覆盖即可，设备数量是个位数，留着没有成本。
        async with self._lock:
            if self._devices.get(conn.device_id) is conn:
                # operator_off 是操作员主动关闭：状态保留，且**不允许被后续的
                # socket 断开降级**。bye 帧和 TCP close 会先后到达，谁后到谁生效
                # 会把 operator_off 冲成 offline，UI 就说不清"是人关的"。
                if conn.info.state != "operator_off":
                    conn.info.state = "operator_off" if reason == "operator_off" else "offline"
                conn.info.last_seen = int(time.time())
        for p in list(conn.pending.values()):
            if not p.future.done():
                p.future.set_exception(RuntimeError(f"device went away: {reason}"))
        conn.pending.clear()
        logger.info("mac_bridge: device %s -> %s (%s)", conn.device_id, conn.info.state, reason)

    def touch(self, conn: DeviceConn) -> None:
        conn.last_seen = time.monotonic()
        conn.info.last_seen = int(time.time())

    # ── 请求 ────────────────────────────────────────────────────────────
    async def call(
        self,
        *,
        op: str,
        args: dict[str, Any],
        device_id: str = "",
        timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS,
    ) -> CallResponse:
        conn = self._pick(device_id)
        if conn is None:
            return CallResponse(ok=False, code="E_NO_DEVICE", error="没有在线的设备（开关是否打开？）")
        if conn.info.state != "online":
            return CallResponse(
                ok=False, code="E_DISABLED",
                error=f"设备 {conn.device_id} 当前是 {conn.info.state} 状态，请在 Mac 上打开开关",
            )

        timeout_ms = max(1_000, min(timeout_ms, MAX_CALL_TIMEOUT_MS))
        req_id = "r" + uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        frame: dict[str, Any] = {"v": 1, "id": req_id, "type": op, "ts": int(time.time()), "payload": args}
        async with self._lock:
            conn.pending[req_id] = _Pending(future=fut, created=time.monotonic())
        try:
            await conn.send(frame)
        except Exception as exc:  # noqa: BLE001 - 发送失败要翻译成调用方能懂的错误
            async with self._lock:
                conn.pending.pop(req_id, None)
            return CallResponse(ok=False, code="E_SEND_FAILED", error=str(exc))

        # 网关侧比设备侧多等 10s：设备先按自己的 timeout 杀进程组并回结果，
        # 这 10s 是给它的收尾和网络往返留的余量。
        wait_s = timeout_ms / 1000.0 + 10.0
        try:
            payload = await asyncio.wait_for(fut, timeout=wait_s)
        except asyncio.TimeoutError:
            async with self._lock:
                conn.pending.pop(req_id, None)
            # 让设备把在途的那条命令停掉
            with contextlib.suppress(Exception):
                await conn.send({"v": 1, "id": req_id, "type": "cancel", "payload": {"reason": "gateway timeout"}})
            return CallResponse(ok=False, code="E_TIMEOUT", error=f"设备 {wait_s:.0f}s 未响应")
        except Exception as exc:  # noqa: BLE001
            async with self._lock:
                conn.pending.pop(req_id, None)
            return CallResponse(ok=False, code="E_LINK", error=str(exc))

        async with self._lock:
            conn.pending.pop(req_id, None)

        if not isinstance(payload, dict):
            return CallResponse(ok=False, code="E_BAD_FRAME", error="设备返回了无法解析的结果")
        if payload.get("kind") == "error":
            return CallResponse(
                ok=False,
                device_id=conn.device_id,
                code=str(payload.get("code") or "E_DEVICE"),
                error=str(payload.get("message") or "device error"),
            )
        return CallResponse(ok=True, device_id=conn.device_id, result=payload)

    # ── 帧分发（由 WS 端点调用）────────────────────────────────────────
    async def on_frame(self, conn: DeviceConn, raw: str | bytes) -> None:
        self.touch(conn)
        try:
            frame = json.loads(raw)
        except Exception:  # noqa: BLE001
            logger.warning("mac_bridge: undecodable frame from %s", conn.device_id)
            return
        if not isinstance(frame, dict):
            return

        # 信封约定：id/type 在顶层，业务字段全在 payload 里。
        # 早期版本直接在顶层找 ok/stdout，于是 result 帧全被当成"无 id"丢掉。
        body = frame.get("payload")
        if not isinstance(body, dict):
            body = {}

        ftype = frame.get("type")
        if ftype == "ping":
            await conn.send({"v": 1, "type": "pong", "ts": int(time.time())})
            return

        if ftype == "bye":
            await self.detach(conn, reason=str(body.get("reason") or frame.get("reason") or "bye"))
            return

        req_id = str(frame.get("id") or "")
        if not req_id:
            return

        entry = conn.pending.get(req_id)
        if entry is None or entry.future.done():
            return

        if ftype == "result":
            payload = {
                "kind": "result",
                "ok": bool(body.get("ok", True)),
                "stdout": body.get("stdout", ""),
                "stderr": body.get("stderr", ""),
                "exit_code": int(body.get("exit_code", 0) or 0),
                "truncated_bytes": int(body.get("truncated_bytes", 0) or 0),
                "duration_ms": int(body.get("duration_ms", 0) or 0),
            }
            if body.get("rewritten"):
                payload["rewritten"] = body["rewritten"]
            entry.future.set_result(payload)
        elif ftype == "error":
            entry.future.set_result({
                "kind": "error",
                "code": body.get("code", "E_DEVICE"),
                "message": body.get("message", "device error"),
            })
        elif ftype == "chunk":
            # pull 的分片：v1 直接累计，将来要流式再改
            entry.future.set_result({
                "kind": "chunk",
                "seq": int(body.get("seq", 0) or 0),
                "data": body.get("data", ""),
                "last": bool(body.get("last")),
                "total": int(body.get("total", 0) or 0),
            })

    # ── 查询 ────────────────────────────────────────────────────────────
    def list_devices(self) -> list[DeviceInfo]:
        return [c.info for c in self._devices.values()]

    def _pick(self, device_id: str) -> DeviceConn | None:
        # 只挑 online 的。注册表里可能还留着 operator_off / offline 的记录，
        # 那些是"状态展示用"，不能再接请求。
        if device_id:
            conn = self._devices.get(device_id)
            return conn if conn is not None and conn.info.state == "online" else None
        online = [c for c in self._devices.values() if c.info.state == "online"]
        return online[0] if len(online) == 1 else None

    # ── 失活巡检 ────────────────────────────────────────────────────────
    def _ensure_watchdog(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = loop.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(STALE_SEC / 3)
            now = time.monotonic()
            stale = [c for c in list(self._devices.values()) if now - c.last_seen > STALE_SEC]
            for conn in stale:
                logger.warning("mac_bridge: device %s unresponsive, failing %d pending",
                               conn.device_id, len(conn.pending))
                # 连接还在但进程冻结了（macOS 合盖/被 SIGSTOP）。直接判离线，
                # 让调用方立刻拿到错误，而不是各自等到超时。
                with contextlib.suppress(Exception):
                    await conn.close()
                await self.detach(conn, reason="unresponsive")

    async def shutdown(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        for conn in list(self._devices.values()):
            with contextlib.suppress(Exception):
                await conn.close()
            await self.detach(conn, reason="shutdown")


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode(text or "")
