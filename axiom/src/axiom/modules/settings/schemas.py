from __future__ import annotations

import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from axiom.modules.guard.defaults import (
    HARD_BLOCK_RULES,
    SYSTEM_PROMPT,
    WHITELIST_BASH_VERBS,
    WHITELIST_TOOLS,
)


class GuardHardBlockRule(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    pattern: str = Field(min_length=1, max_length=2_000)
    enabled: bool = True

    @field_validator("reason", "pattern")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid guard regex: {exc}") from exc
        return value


class RuntimeConfig(BaseModel):
    runner_interval_seconds: float = Field(default=3.0, gt=0, le=60)
    global_max_active_runs: int = Field(
        default=10,
        ge=1,
        le=128,
        validation_alias=AliasChoices("global_max_active_runs", "max_parallel"),
    )
    global_max_active_tasks: int = Field(default=3, ge=1, le=128)
    per_task_max_active_runs: int = Field(default=4, ge=1, le=128)
    per_task_explore_concurrency: int = Field(default=4, ge=1, le=128)
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=300)
    lease_timeout_seconds: int = Field(default=60, ge=20, le=3_600)
    execution_retry_limit: int = Field(default=6, ge=1, le=50)
    infrastructure_retry_limit: int = Field(default=30, ge=1, le=200)
    retry_base_seconds: int = Field(default=15, ge=1, le=3_600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    worker_failure_cooldown_seconds: int = Field(default=30, ge=1, le=3_600)
    bootstrap_execute_seconds: int = Field(default=300, ge=60, le=86_400)
    bootstrap_conclude_seconds: int = Field(default=120, ge=30, le=7_200)
    reason_execute_seconds: int = Field(default=300, ge=60, le=7_200)
    reason_conclude_seconds: int = Field(default=120, ge=30, le=7_200)
    reason_batch_size: int = Field(default=2, ge=1, le=20)
    reason_low_watermark: int = Field(default=1, ge=0, le=20)
    reason_max_wait_seconds: int = Field(default=180, ge=10, le=3_600)
    reason_min_interval_seconds: int = Field(default=60, ge=0, le=3_600)
    reason_target_open_intents: int = Field(default=4, ge=1, le=8)
    reason_max_new_intents_per_run: int = Field(default=4, ge=1, le=8)
    explore_execute_min_seconds: int = Field(default=60, ge=10, le=86_400)
    explore_execute_max_seconds: int = Field(default=3_600, ge=10, le=86_400)
    explore_conclude_min_seconds: int = Field(default=30, ge=10, le=7_200)
    explore_conclude_max_seconds: int = Field(default=600, ge=10, le=7_200)
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
    guard_whitelist_tools: list[str] = Field(
        default_factory=lambda: list(WHITELIST_TOOLS),
        max_length=128,
    )
    guard_whitelist_bash_verbs: list[str] = Field(
        default_factory=lambda: list(WHITELIST_BASH_VERBS),
        max_length=128,
    )
    guard_hard_block_rules: list[GuardHardBlockRule] = Field(
        default_factory=lambda: [GuardHardBlockRule.model_validate(rule) for rule in HARD_BLOCK_RULES],
        max_length=128,
    )
    guard_system_prompt: str = Field(default=SYSTEM_PROMPT, max_length=20_000)

    @field_validator("guard_whitelist_tools", "guard_whitelist_bash_verbs")
    @classmethod
    def normalize_guard_allowlist(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                value.strip().casefold()
                for value in values
                if isinstance(value, str) and value.strip()
            )
        )

    @model_validator(mode="after")
    def validate_relationships(self) -> "RuntimeConfig":
        if self.global_max_active_tasks > self.global_max_active_runs:
            raise ValueError("global_max_active_tasks cannot exceed global_max_active_runs")
        if self.per_task_max_active_runs > self.global_max_active_runs:
            raise ValueError("per_task_max_active_runs cannot exceed global_max_active_runs")
        if self.per_task_explore_concurrency > self.per_task_max_active_runs:
            raise ValueError(
                "per_task_explore_concurrency cannot exceed per_task_max_active_runs"
            )
        if self.lease_timeout_seconds < self.heartbeat_interval_seconds * 3:
            raise ValueError("lease_timeout_seconds must allow at least three heartbeats")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds cannot be smaller than retry_base_seconds")
        if self.network_proxy_mode != "off" and not self.network_proxy_url.strip():
            raise ValueError("network_proxy_url is required when proxy mode is enabled")
        if self.reason_max_new_intents_per_run > self.reason_target_open_intents:
            raise ValueError(
                "reason_max_new_intents_per_run cannot exceed reason_target_open_intents"
            )
        if self.explore_execute_min_seconds > self.explore_execute_max_seconds:
            raise ValueError("explore execute minimum cannot exceed maximum")
        if self.explore_conclude_min_seconds > self.explore_conclude_max_seconds:
            raise ValueError("explore conclude minimum cannot exceed maximum")
        return self


class RuntimeSettings(BaseModel):
    config: RuntimeConfig
    version: int
    updated_by: str
    updated_at: str
    applied_version: int = 0
    applied_at: str | None = None


class UpdateRuntimeSettings(BaseModel):
    config: RuntimeConfig
    expected_version: int = Field(ge=1)
    actor: str = "human"


class ApplyRuntimeSettings(BaseModel):
    version: int = Field(ge=1)
