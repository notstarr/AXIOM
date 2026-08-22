from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class GuardRequest(BaseModel):
    project_id: str | None = None
    task_id: str | None = None
    intent_id: str | None = None
    reason_run_id: str | None = None
    plan_run_id: str | None = None
    review_run_id: str | None = None
    phase: str = ""
    worker: str = ""
    tool_name: str
    tool_call_id: str = ""
    input: Any = Field(default_factory=dict)
    cwd: str = ""

    @field_validator("tool_name")
    @classmethod
    def require_tool(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("tool_name must not be empty")
        return value

    @field_validator("project_id", "task_id", "intent_id", "reason_run_id", "plan_run_id", "review_run_id")
    @classmethod
    def normalize_owner_ids(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class GuardDecision(BaseModel):
    decision: Literal["ALLOW", "BLOCK"]
    reason: str = ""
    source: Literal["disabled", "whitelist", "hard_rule", "model", "fail_open"]
    observed_decision: Literal["ALLOW", "BLOCK"] | None = None
    observed_reason: str = ""
    event_id: str = ""


class GuardDefaults(BaseModel):
    whitelist_tools: list[str]
    whitelist_bash_verbs: list[str]
    hard_block_rules: list[dict[str, Any]]
    system_prompt: str
