from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


WorkerCapability = Literal["bootstrap", "reason", "explore", "plan", "review"]


class UpsertWorker(BaseModel):
    worker_type: str = "pi"
    capabilities: list[WorkerCapability] = Field(
        # New Workers opt into Plan/Review explicitly.  The compatibility
        # migration grants Plan to pre-existing Reason Workers once, without
        # coupling those capabilities for future Worker creation.
        default_factory=lambda: ["bootstrap", "reason", "explore"]
    )
    max_running: int = Field(default=1, ge=1, le=128)
    priority: int = Field(default=0, ge=-10_000, le=10_000)
    config: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False

    @field_validator("worker_type")
    @classmethod
    def require_worker_type(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("worker_type must not be empty")
        return value

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, values: list[WorkerCapability]) -> list[WorkerCapability]:
        return list(dict.fromkeys(values))


class Worker(BaseModel):
    name: str
    worker_type: str
    capabilities: list[WorkerCapability]
    max_running: int
    priority: int
    config: dict[str, Any]
    disabled: bool
    cooldown_until: str | None = None
    # Whether the gateway accepts OpenAI's ``developer`` message role.
    # NULL = not probed yet, FALSE = confirmed refused (fall back to ``system``).
    developer_role_supported: bool | None = None
    created_at: str
    updated_at: str


class WorkerRename(BaseModel):
    new_name: str


class WorkerCooldown(BaseModel):
    cooldown_until: str | None = None


class WorkerCapabilityReport(BaseModel):
    """A probed gateway capability reported by the Runner."""

    developer_role_supported: bool | None = None


class WorkerConnectionRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    provider_api: str = ""
    # 草稿态开关：未保存也能按该语义测试连通性（持久化后以 worker 配置为准）
    llm_proxy: bool = False


class WorkerConnectionResult(BaseModel):
    ok: bool
    message: str


class WorkerModel(BaseModel):
    id: str
    name: str
    context_window: int = 200_000


class WorkerModelsResult(BaseModel):
    models: list[WorkerModel] = Field(default_factory=list)
    error: str = ""
