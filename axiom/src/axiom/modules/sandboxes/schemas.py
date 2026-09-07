from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SandboxStatus = Literal["creating", "ready", "degraded", "terminating", "terminated", "failed"]


class TaskSandbox(BaseModel):
    id: str
    task_id: str
    sandbox_id: str
    backend: str
    status: SandboxStatus
    template_version: str
    runtime_revision: str = "legacy-local"
    remote_root: str = "/workspace/axiom"
    resource_profile: dict[str, Any] = Field(default_factory=dict)
    network_policy: dict[str, Any] = Field(default_factory=dict)
    active_worker_count: int = 0
    created_at: str
    last_used_at: str
    terminated_at: str | None = None
    last_error: str = ""
    last_healthcheck_at: str | None = None
    last_healthcheck_error: str = ""


class SandboxHandle(BaseModel):
    sandbox_id: str
    root_path: str
    backend: str
    metadata: dict[str, Any] = Field(default_factory=dict)
