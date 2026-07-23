from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RuntimeConfig(BaseModel):
    runner_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    max_parallel: int = Field(default=8, ge=1, le=128)
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=300)
    lease_timeout_seconds: int = Field(default=60, ge=20, le=3_600)
    execution_retry_limit: int = Field(default=6, ge=1, le=50)
    infrastructure_retry_limit: int = Field(default=30, ge=1, le=200)
    retry_base_seconds: int = Field(default=15, ge=1, le=3_600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    worker_failure_cooldown_seconds: int = Field(default=30, ge=1, le=3_600)
    bootstrap_execute_seconds: int = Field(default=1_800, ge=60, le=86_400)
    bootstrap_conclude_seconds: int = Field(default=600, ge=30, le=7_200)
    reason_execute_seconds: int = Field(default=900, ge=60, le=7_200)
    reason_batch_size: int = Field(default=2, ge=1, le=20)
    reason_low_watermark: int = Field(default=1, ge=0, le=20)
    reason_max_wait_seconds: int = Field(default=180, ge=10, le=3_600)
    reason_target_open_intents: int = Field(default=4, ge=1, le=8)
    network_proxy_mode: Literal["off", "http", "vpn"] = "off"
    network_proxy_url: str = "http://127.0.0.1:7890"
    external_report_base_url: str = ""
    external_report_token: str = ""
    external_report_timeout_seconds: int = Field(default=15, ge=1, le=120)
    external_report_max_attempts: int = Field(default=3, ge=1, le=10)
    guard_mode: Literal["off", "audit", "enforce"] = "enforce"
    guard_enabled_default: bool = False
    guard_model_base_url: str = ""
    guard_model_name: str = ""
    guard_api_key: str = ""
    guard_timeout_seconds: int = Field(default=3, ge=1, le=30)

    @model_validator(mode="after")
    def validate_relationships(self) -> "RuntimeConfig":
        if self.lease_timeout_seconds < self.heartbeat_interval_seconds * 3:
            raise ValueError("lease_timeout_seconds must allow at least three heartbeats")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds cannot be smaller than retry_base_seconds")
        if self.network_proxy_mode != "off" and not self.network_proxy_url.strip():
            raise ValueError("network_proxy_url is required when proxy mode is enabled")
        return self


class RuntimeSettings(BaseModel):
    config: RuntimeConfig
    version: int
    updated_by: str
    updated_at: str


class UpdateRuntimeSettings(BaseModel):
    config: RuntimeConfig
    actor: str = "human"
