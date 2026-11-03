from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.mac_bridge.schemas import (
    CallRequest,
    CallResponse,
    DeviceInfo,
    DevicesResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mac-bridge"])


# ---------------------------------------------------------------------------
# 设备侧 WebSocket
#
# 重要：app.py 的鉴权是 `@app.middleware("http")`，Starlette 的 HTTP 中间件
# 不覆盖 websocket scope。所以这条路由必须自己校验 token，而且
# AXIOM_AUTH_DISABLED=1 也不能影响它——那是个只给本地开发用的开关，
# 不该让设备端口对公网敞开。
# ---------------------------------------------------------------------------
@router.websocket("/mac-bridge/device")
async def device_socket(websocket: WebSocket) -> None:
    container: Container = websocket.app.state.container
    bridge = container.mac_bridge

    presented = (
        websocket.query_params.get("token", "")
        or (websocket.headers.get("authorization", "").removeprefix("Bearer ").strip())
        or ""
    )
    if not bridge.verify_device_token(presented):
        logger.warning("mac_bridge: device handshake rejected (bad token)")
        await websocket.close(code=4401)
        return

    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send(frame: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(frame, ensure_ascii=False))

    async def close() -> None:
        with contextlib.suppress(Exception):
            await websocket.close()

    # hello 必须是第一帧
    conn = None
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        frame = json.loads(first)
        if frame.get("type") != "hello":
            await close()
            return
        # 业务字段在 payload 里，不在信封顶层
        body = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            await close()
            return
        info = DeviceInfo(
            device_id=device_id,
            hostname=str(body.get("hostname") or ""),
            os=str(body.get("os") or ""),
            arch=str(body.get("arch") or ""),
            app_version=str(body.get("app_version") or ""),
        )
        conn = await bridge.attach(device_id=device_id, info=info, send=send, close=close)
        await send({
            "v": 1,
            "type": "hello_ok",
            "ts": int(time.time()),
            "payload": {
                "heartbeat_sec": 10,
                "max_frame": 1 << 20,
                "max_pull": 32 << 20,
            },
        })
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        await close()
        return

    try:
        while True:
            raw = await websocket.receive_text()
            await bridge.on_frame(conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("mac_bridge: socket loop ended for %s", conn.device_id)
    finally:
        if conn is not None:
            await bridge.detach(conn, reason="socket closed")


# ---------------------------------------------------------------------------
# 工具侧内部调用
#
# 这里刻意只认 service_token：工具包跑在 worker 进程里，用的是机器凭据。
# 放开成"用户 token 也行"会让一个浏览器会话能直接在这台 Mac 上执行命令。
# ---------------------------------------------------------------------------
def _require_service_token(request: Request) -> None:
    presented = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    expected = getattr(request.app.state, "service_token", "") or ""
    if not expected or not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="service token required")


@router.post("/internal/mac-bridge/call", response_model=CallResponse)
async def internal_call(
    body: CallRequest,
    request: Request,
    _: None = Depends(_require_service_token),
    container: Container = Depends(get_container),
) -> CallResponse:
    return await container.mac_bridge.call(
        op=body.op,
        args=body.args,
        device_id=body.device_id,
        timeout_ms=body.timeout_ms,
    )


@router.get("/mac-bridge/devices", response_model=DevicesResponse)
def list_devices(container: Container = Depends(get_container)) -> DevicesResponse:
    return DevicesResponse(devices=container.mac_bridge.list_devices())
