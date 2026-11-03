from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


DeviceState = Literal["online", "offline", "operator_off"]


class DeviceInfo(BaseModel):
    device_id: str
    hostname: str = ""
    os: str = ""
    arch: str = ""
    app_version: str = ""
    state: DeviceState = "offline"
    last_seen: int = 0
    connected_at: int = 0


class CallRequest(BaseModel):
    """工具侧的一次调用。``op`` 只有三种，刻意少——能力在设备的 shell 里。"""

    op: Literal["exec", "pull", "stat"] = "exec"
    device_id: str = Field(default="", description="留空 = 用唯一在线设备")
    args: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=60_000, ge=1_000, le=600_000)


class CallResponse(BaseModel):
    ok: bool
    device_id: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    code: str = ""


class DevicesResponse(BaseModel):
    devices: list[DeviceInfo]
