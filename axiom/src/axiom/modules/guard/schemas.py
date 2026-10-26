from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


GuardScriptStatus = Literal["complete", "incomplete", "not_applicable"]


class GuardScript(BaseModel):
    """One read-only snapshot of a script the tool call will execute.

    ``sha256`` is computed by the server from ``content`` rather than accepted
    from the client, so the digest in the audit record always describes the text
    the judge actually read.  ``read_error`` is what makes an unresolved reference
    honest: the judge is told the body is missing instead of being handed an
    empty snapshot that looks like "nothing to see".
    """

    path: str = ""
    source: Literal["file", "command"] = "file"
    content: str = ""
    bytes: int = 0
    complete: bool = False
    truncated: bool = False
    read_error: str = ""


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
    # Read-only snapshots of the scripts this call will run, captured by the
    # Worker-side extension (see guard.scripts for why the Worker reads them).
    scripts: list[GuardScript] = Field(default_factory=list)
    script_capture_status: GuardScriptStatus = "not_applicable"

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
    # ``reason`` is the audit-facing explanation the operator investigates with.
    reason: str = ""
    # ``message`` is the model-facing text the Worker receives.  On a block it
    # carries the platform framing from :func:`guard.messages.platform_block`,
    # so the Worker does not mistake a policy decision for target-side defense.
    message: str = ""
    source: Literal["disabled", "whitelist", "hard_rule", "model", "fail_open"]
    observed_decision: Literal["ALLOW", "BLOCK"] | None = None
    observed_reason: str = ""
    event_id: str = ""
    # Diagnostics for the audit log: how long the judge took, and what it
    # literally said.  Both are only set on the model path.  ``judge_input`` is
    # the masked text that was actually sent - it is what proves no credential
    # left the host - and ``redacted_count`` is how many values were replaced.
    model_latency_ms: int = 0
    raw_model_output: str = ""
    judge_input: str = ""
    redacted_count: int = 0


class GuardProfileDefaults(BaseModel):
    system_prompt: str
    hard_block_rules: list[dict[str, Any]]


class GuardDefaults(BaseModel):
    whitelist_tools: list[str]
    whitelist_bash_verbs: list[str]
    hard_block_rules: list[dict[str, Any]]
    system_prompt: str
    # Shipped per-mode policy, so the settings page can offer "restore defaults"
    # for whichever mode the operator is editing.
    profiles: dict[str, GuardProfileDefaults] = Field(default_factory=dict)
