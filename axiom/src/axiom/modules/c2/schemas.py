from __future__ import annotations

import ipaddress
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Listeners bind loopback only; Caddy terminates TLS and reverse proxies /c2/.
# TCP is not offered: the raw socket has no handshake and no token, so anyone
# who finds the port can take the session and the commands with it.
LISTENER_HOST = "127.0.0.1"


class CreateListener(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["http"] = "http"
    host: str = LISTENER_HOST
    port: int = Field(ge=1, le=65535)

    @field_validator("name")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("host")
    @classmethod
    def require_loopback(cls, value: str) -> str:
        value = value.strip()
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError(
                f"host 只能是回环地址 {LISTENER_HOST} 或 ::1；对外由 Caddy 反代 /c2/"
            ) from None
        if not address.is_loopback:
            raise ValueError(
                f"host 只能是回环地址 {LISTENER_HOST} 或 ::1；对外由 Caddy 反代 /c2/"
            )
        return str(address)


class C2Listener(BaseModel):
    id: str
    task_id: str
    name: str
    kind: Literal["http", "tcp"]
    host: str
    port: int
    status: Literal["stopped", "listening", "error"]
    error: str = ""
    created_by: str
    created_at: str
    updated_at: str
    archived: bool = False


class C2Session(BaseModel):
    id: str
    task_id: str
    listener_id: str
    kind: Literal["http", "tcp"]
    remote: str = ""
    info: dict = Field(default_factory=dict)
    status: Literal["active", "dead"]
    sleep_seconds: int = 5
    last_seen_at: str | None = None
    created_at: str
    updated_at: str


class SetSessionSleep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sleep_seconds: int = Field(ge=1, le=3600)


class C2SessionPurge(BaseModel):
    """How many offline sessions a bulk cleanup removed."""

    deleted: int = 0


class C2Task(BaseModel):
    id: str
    task_id: str
    session_id: str
    command: str
    status: Literal["queued", "sent", "completed", "failed", "cancelled"]
    output: str = ""
    duration_ms: int = 0
    actor: str
    intent_id: str | None = None
    created_at: str
    updated_at: str


class C2ExecCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    wait_seconds: int = Field(default=30, ge=0, le=300)

    @field_validator("command")
    @classmethod
    def require_command(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("command must not be empty")
        return value


class C2Oneliner(BaseModel):
    listener_id: str
    kind: Literal["http", "tcp"]
    bash: str = ""
    python: str = ""
    note: str = ""
