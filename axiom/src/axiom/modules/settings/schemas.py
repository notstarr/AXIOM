from __future__ import annotations

import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from axiom.modules.guard.defaults import (
    DEFAULT_GUARD_PROFILES,
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


def _default_guard_profiles() -> dict[str, "GuardProfile"]:
    return {
        mode: GuardProfile.model_validate(profile)
        for mode, profile in DEFAULT_GUARD_PROFILES.items()
    }


class GuardProfile(BaseModel):
    """Per engagement mode overrides.

    The allowlist is deliberately absent: read-only verbs and local file tools
    are the same work in both modes.  Only the rule set and the judge prompt
    change, because vulnerability assessment and red team ask different
    questions about a call.
    """

    system_prompt: str | None = Field(default=None, max_length=20_000)
    hard_block_rules: list["GuardHardBlockRule"] | None = Field(default=None, max_length=128)

    @field_validator("system_prompt")
    @classmethod
    def normalize_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None



class RuntimeConfig(BaseModel):
    runner_interval_seconds: float = Field(default=3.0, gt=0, le=60)
    global_max_active_runs: int = Field(
        default=15,
        ge=1,
        le=128,
        validation_alias=AliasChoices("global_max_active_runs", "max_parallel"),
    )
    global_max_active_tasks: int = Field(default=3, ge=1, le=128)
    per_task_max_active_runs: int = Field(default=10, ge=1, le=128)
    per_task_explore_concurrency: int = Field(default=4, ge=1, le=128)
    per_task_review_concurrency: int = Field(default=4, ge=1, le=128)
    heartbeat_interval_seconds: int = Field(default=10, ge=1, le=300)
    lease_timeout_seconds: int = Field(default=60, ge=20, le=3_600)
    execution_retry_limit: int = Field(default=6, ge=1, le=50)
    infrastructure_retry_limit: int = Field(default=3, ge=1, le=200)
    retry_base_seconds: int = Field(default=30, ge=1, le=3_600)
    retry_max_seconds: int = Field(default=300, ge=1, le=86_400)
    worker_failure_cooldown_seconds: int = Field(default=30, ge=1, le=3_600)
    # Circuit breaker per Worker.  ``worker_failure_cooldown_seconds`` (above) is
    # the first rung; each trip after ``worker_failure_trip_after`` consecutive
    # transient failures multiplies it, up to the ceiling.  A backend quiet for
    # ``worker_breaker_decay_seconds`` has its ladder reset, which is what
    # replaces a success callback.
    worker_failure_trip_after: int = Field(default=2, ge=1, le=50)
    worker_failure_cooling_factor: int = Field(default=5, ge=1, le=100)
    worker_failure_cooldown_max_seconds: int = Field(default=1_800, ge=1, le=86_400)
    worker_breaker_decay_seconds: int = Field(default=3_600, ge=60, le=604_800)
    # Automatic recovery of work parked at its infrastructure retry limit.  A
    # parked row is re-armed once it has been quiet this long, which converts an
    # upstream hiccup from a dead Task into a few lost minutes.  ``max_rounds``
    # bounds how many times automation may do that before leaving the row for a
    # human, so a permanently broken backend cannot retry forever.
    retry_recovery_enabled: bool = True
    retry_recovery_quiet_seconds: int = Field(default=900, ge=60, le=86_400)
    retry_recovery_max_rounds: int = Field(default=5, ge=0, le=100)
    # How long a running Task may sit with claimable-looking work and no claims
    # before the Runner records a ``task.dispatch_stalled`` warning.  This is
    # visibility only — it never changes scheduling.  Keep it above the retry
    # recovery quiet window so recovery gets its chance first.
    dispatch_stall_seconds: int = Field(default=1_800, ge=60, le=86_400)
    bootstrap_execute_seconds: int = Field(default=600, ge=60, le=86_400)
    bootstrap_conclude_seconds: int = Field(default=180, ge=30, le=7_200)
    reason_execute_seconds: int = Field(default=600, ge=60, le=7_200)
    reason_conclude_seconds: int = Field(default=300, ge=30, le=7_200)
    # Plan is an independent runtime.  Keep its budgets separate from
    # Reason so a long-running panorama pass cannot silently inherit a
    # Reason tuning change (or vice versa).
    plan_execute_seconds: int = Field(default=600, ge=60, le=7_200)
    plan_conclude_seconds: int = Field(default=300, ge=30, le=7_200)
    review_execute_seconds: int = Field(default=400, ge=60, le=7_200)
    review_conclude_seconds: int = Field(default=180, ge=30, le=7_200)
    reason_batch_size: int = Field(default=2, ge=1, le=20)
    reason_low_watermark: int = Field(default=1, ge=0, le=20)
    reason_max_wait_seconds: int = Field(default=180, ge=10, le=3_600)
    reason_min_interval_seconds: int = Field(default=60, ge=0, le=3_600)
    # At least one pending slot keeps the queue semantics lossless.  Legacy
    # persisted values of 0 are normalized by the pre-validator below.
    reason_queue_limit: int = Field(default=3, ge=1, le=10)
    plan_queue_limit: int = Field(default=3, ge=1, le=10)
    plan_max_consecutive_runs: int = Field(default=3, ge=1, le=20)
    explore_execute_min_seconds: int = Field(default=300, ge=10, le=86_400)
    explore_execute_max_seconds: int = Field(default=3_600, ge=10, le=86_400)
    explore_execute_default_seconds: int = Field(default=900, ge=10, le=86_400)
    explore_conclude_min_seconds: int = Field(default=180, ge=180, le=7_200)
    explore_conclude_max_seconds: int = Field(default=600, ge=10, le=7_200)
    explore_conclude_default_seconds: int = Field(default=300, ge=10, le=7_200)
    explore_no_progress_seconds: int = Field(default=750, ge=30, le=7_200)
    sandbox_backend: Literal["local", "cube"] = "local"
    sandbox_required: bool = False
    sandbox_local_fallback: bool = True
    sandbox_template: str = "axiom-worker"
    sandbox_runtime_revision: str = "legacy-local"
    sandbox_healthcheck_timeout: int = Field(default=10, ge=1, le=300)
    browser_gateway_enabled: bool = True
    browser_gateway_bind: str = "127.0.0.1"
    browser_gateway_base_url: str = ""
    browser_file_bridge_limit: int = Field(default=2 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024)
    network_proxy_mode: Literal["off", "http", "vpn"] = "off"
    network_proxy_url: str = "http://127.0.0.1:7890"
    guard_mode: Literal["off", "audit", "enforce"] = "enforce"
    guard_enabled_default: bool = False
    guard_model_base_url: str = "https://api.siliconflow.cn/v1"
    guard_model_name: str = "Qwen/Qwen3.5-9B"
    guard_api_key: str = ""
    guard_timeout_seconds: int = Field(default=3, ge=1, le=30)
    # Reasoning models spend max_tokens on think tokens before emitting the
    # verdict digit, which turns a 0.5s answer into 5-12s and often times out.
    # The guard contract needs one character, so thinking is off by default.
    guard_thinking_enabled: bool = False
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
    # Per engagement mode overrides.  ``vulnerability`` and ``red_team`` ship
    # with different rule sets and prompts; a mode missing here falls back to
    # the fields above, so older configurations keep working unchanged.
    guard_profiles: dict[str, GuardProfile] = Field(default_factory=_default_guard_profiles)

    @field_validator("reason_queue_limit", mode="before")
    @classmethod
    def normalize_reason_queue_limit(cls, value: int | str | None) -> int:
        if value is None or value == "":
            return 3
        # Legacy installations persisted 0.  A lossless queue always keeps
        # at least one pending slot, so normalize that value on read.
        return max(1, int(value))

    @field_validator("plan_queue_limit", "plan_max_consecutive_runs", mode="before")
    @classmethod
    def normalize_plan_queue_limit(cls, value: int | str | None) -> int:
        if value is None or value == "":
            return 3
        parsed = int(value)
        return 1 if parsed == 0 else parsed

    # 钉钉通知与 Webhook 配置
    dingtalk_enabled: bool = False
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""
    # 红队成果只发业务方简版卡（时间·业务·动作），不带漏洞描述/证据/积分等内部细节。
    dingtalk_redteam_public_only: bool = False
    # 钉钉 Stream 模式机器人（企业内部应用），用于接收群消息并响应 /help 等指令
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""

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
        if self.sandbox_required and self.sandbox_backend != "cube":
            raise ValueError("sandbox_required requires sandbox_backend='cube'")
        if self.sandbox_backend == "cube" and self.sandbox_required and self.sandbox_local_fallback:
            raise ValueError("sandbox_required disables sandbox_local_fallback")
        if (
            self.sandbox_backend == "cube"
            and self.sandbox_required
            and self.browser_gateway_enabled
            and not self.browser_gateway_base_url.strip()
        ):
            raise ValueError(
                "browser_gateway_base_url is required when Cube workers use the Browser Gateway"
            )
        if self.global_max_active_tasks > self.global_max_active_runs:
            raise ValueError("global_max_active_tasks cannot exceed global_max_active_runs")
        if self.per_task_max_active_runs > self.global_max_active_runs:
            raise ValueError("per_task_max_active_runs cannot exceed global_max_active_runs")
        if self.per_task_explore_concurrency > self.per_task_max_active_runs:
            raise ValueError(
                "per_task_explore_concurrency cannot exceed per_task_max_active_runs"
            )
        if self.per_task_review_concurrency > self.per_task_max_active_runs:
            raise ValueError(
                "per_task_review_concurrency cannot exceed per_task_max_active_runs"
            )
        if self.lease_timeout_seconds < self.heartbeat_interval_seconds * 3:
            raise ValueError("lease_timeout_seconds must allow at least three heartbeats")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds cannot be smaller than retry_base_seconds")
        if self.worker_failure_cooldown_max_seconds < self.worker_failure_cooldown_seconds:
            raise ValueError(
                "worker_failure_cooldown_max_seconds cannot be smaller than "
                "worker_failure_cooldown_seconds"
            )
        if self.worker_breaker_decay_seconds <= self.worker_failure_cooldown_max_seconds:
            # A ladder that cannot be outlived is a ladder that never resets: the
            # decay window has to exceed the longest cooldown, or a backend that
            # tripped at the ceiling is compared against a window it can never
            # fill and keeps its escalated state forever.
            raise ValueError(
                "worker_breaker_decay_seconds must be greater than "
                "worker_failure_cooldown_max_seconds"
            )
        if self.network_proxy_mode != "off" and not self.network_proxy_url.strip():
            raise ValueError("network_proxy_url is required when proxy mode is enabled")
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
