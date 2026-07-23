from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from axiom.guard.policy import (
    DEFAULT_HARD_BLOCK_RULES,
    DEFAULT_WHITELIST_BASH_VERBS,
    DEFAULT_WHITELIST_TOOLS,
    MODEL_SYSTEM_PROMPT,
)


class RuntimeConfig(BaseModel):
    interval_seconds: int = Field(default=1, gt=0)
    max_workers: int = Field(default=4, gt=0)
    max_running_tasks: int = Field(default=2, gt=0)
    max_task_workers: int = Field(default=2, gt=0)
    bootstrap_limit: int = Field(default=1, gt=0)
    reason_limit: int = Field(default=1, gt=0)
    explore_limit: int = Field(default=2, gt=0)
    crawler_enabled: bool = False
    mozzie_base_url: str = "https://mozzie.t.cn-shenzhen.aliyun.kkgroup.work"
    mozzie_token: str = ""
    mozzie_timeout_seconds: int = Field(default=15, gt=0, le=120)
    external_report_max_attempts: int = Field(default=3, gt=0, le=10)
    heartbeat_interval_seconds: int = Field(default=10, gt=0, le=300)
    lease_timeout_seconds: int = Field(default=60, gt=0, le=3600)
    retry_base_seconds: int = Field(default=30, gt=0, le=3600)
    worker_failure_cooldown_seconds: int = Field(default=30, gt=0, le=3600)
    bootstrap_execute_seconds: int = Field(default=1800, gt=0)
    bootstrap_conclude_seconds: int = Field(default=600, gt=0)
    reason_execute_seconds: int = Field(default=900, gt=0)
    # Incremental Reason is deliberately batched.  A single completed Intent
    # should not immediately steal a Worker from Explore, but the queue must
    # still be replenished before it drains or a result waits indefinitely.
    reason_batch_size: int = Field(default=2, ge=1, le=20)
    reason_low_watermark: int = Field(default=1, ge=0, le=20)
    reason_max_wait_seconds: int = Field(default=180, gt=0, le=3600)
    reason_min_interval_seconds: int = Field(default=60, ge=0, le=3600)
    reason_target_open_intents: int = Field(default=4, ge=1, le=4)
    explore_execute_seconds: int = Field(default=1800, gt=0)
    explore_conclude_seconds: int = Field(default=900, gt=0)
    guard_mode: str = "enforce"
    guard_enabled_default: bool = False
    guard_model_base_url: str = "https://api.siliconflow.cn/v1"
    guard_model_name: str = "Qwen/Qwen3.5-9B"
    guard_api_key: str = ""
    guard_timeout_seconds: int = Field(default=3, gt=0, le=30)
    guard_system_prompt: str = MODEL_SYSTEM_PROMPT
    guard_whitelist_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_WHITELIST_TOOLS))
    guard_whitelist_bash_verbs: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WHITELIST_BASH_VERBS)
    )
    guard_hard_block_rules: list[dict[str, object]] = Field(
        default_factory=lambda: [dict(item) for item in DEFAULT_HARD_BLOCK_RULES]
    )

    @model_validator(mode="after")
    def validate_limits(self) -> "RuntimeConfig":
        if self.max_task_workers > self.max_workers:
            raise ValueError("max_task_workers cannot exceed max_workers")
        return self


class RuntimeSettings(BaseModel):
    config: RuntimeConfig
    version: int
    updated_by: str
    updated_at: str
