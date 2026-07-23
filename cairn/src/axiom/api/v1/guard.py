from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.guard.policy import (
    DEFAULT_HARD_BLOCK_RULES,
    DEFAULT_WHITELIST_BASH_VERBS,
    DEFAULT_WHITELIST_TOOLS,
    MODEL_SYSTEM_PROMPT,
    GuardPolicyConfig,
    evaluate_guard_event,
    normalize_tool_input,
)
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.settings.service import RuntimeSettingsService


router = APIRouter(
    prefix="/internal/guard",
    tags=["v1-internal-guard"],
    dependencies=[Depends(require_service_token)],
)
public_router = APIRouter(prefix="/guard", tags=["v1-guard-ui"])


class GuardRequest(BaseModel):
    project_id: str | None = None
    task_id: str | None = None
    intent_id: str | None = None
    phase: str = ""
    worker: str = ""
    tool_name: str
    tool_call_id: str = ""
    input: Any = Field(default_factory=dict)
    cwd: str = ""


class GuardDecision(BaseModel):
    decision: str
    reason: str = ""
    source: str


class GuardDefaults(BaseModel):
    system_prompt: str
    whitelist_tools: list[str]
    whitelist_bash_verbs: list[str]
    hard_block_rules: list[dict[str, Any]]


@router.post("/pre", response_model=GuardDecision)
def guard_pre(body: GuardRequest, engine=Depends(get_v2_engine)) -> GuardDecision:
    return _evaluate_guard(body, engine)


@public_router.get("/defaults", response_model=GuardDefaults)
def guard_defaults() -> GuardDefaults:
    return GuardDefaults(
        system_prompt=MODEL_SYSTEM_PROMPT,
        whitelist_tools=list(DEFAULT_WHITELIST_TOOLS),
        whitelist_bash_verbs=list(DEFAULT_WHITELIST_BASH_VERBS),
        hard_block_rules=[dict(item) for item in DEFAULT_HARD_BLOCK_RULES],
    )


def _evaluate_guard(body: GuardRequest, engine) -> GuardDecision:
    normalized = normalize_tool_input(body.tool_name, body.input)
    model_name = ""
    model_latency_ms = 0
    raw_model_output = ""
    deterministic = _deterministic_decision(body)
    if deterministic is not None:
        decision = deterministic
    elif not _is_external_operation(body.tool_name, body.input):
        decision = GuardDecision(decision="ALLOW", reason="local operation", source="whitelist")
    else:
        runtime = RuntimeSettingsService(engine).get().config
        config = GuardPolicyConfig(
            mode=runtime.guard_mode,  # type: ignore[arg-type]
            base_url=runtime.guard_model_base_url,
            model_name=runtime.guard_model_name,
            timeout_seconds=runtime.guard_timeout_seconds,
            whitelist_tools=runtime.guard_whitelist_tools,
            whitelist_bash_verbs=runtime.guard_whitelist_bash_verbs,
            system_prompt=runtime.guard_system_prompt,
            hard_block_rules=runtime.guard_hard_block_rules,
        )
        result = evaluate_guard_event(
            tool_name=body.tool_name,
            input_payload=body.input,
            metadata=body.model_dump(exclude={"input"}),
            config=config,
            api_key=runtime.guard_api_key,
        )
        decision = GuardDecision(
            decision=result.decision,
            reason=result.reason,
            source=result.source,
        )
        normalized = result.normalized
        model_name = result.model_name
        model_latency_ms = result.model_latency_ms
        raw_model_output = result.raw_model_output
    AuditService(engine).record(
        RecordAuditEvent(
            project_id=body.project_id,
            task_id=body.task_id,
            intent_id=body.intent_id,
            worker=body.worker,
            event_type="guard.decision",
            level="warning" if decision.decision == "BLOCK" else "debug",
            message=f"Guard {decision.decision}: {body.tool_name}",
            payload={
                "tool_call_id": body.tool_call_id,
                "phase": body.phase,
                "decision": decision.decision,
                "reason": decision.reason,
                "source": decision.source,
                "command": normalized.get("command", ""),
                "input": body.input,
                "normalized": normalized,
                "model_name": model_name,
                "model_latency_ms": model_latency_ms,
                "raw_model_output": raw_model_output,
            },
        )
    )
    return decision


def _deterministic_decision(body: GuardRequest) -> GuardDecision | None:
    if body.tool_name.lower() != "bash":
        return None
    command = str(body.input.get("command") or body.input.get("cmd") or "") if isinstance(body.input, dict) else ""
    if "axiom-tool" not in command:
        return None
    allowed = {
        "reason": ("todo", "intent", "reason", "task complete"),
        "bootstrap": ("credential", "artifact", "vulnerability"),
        "explore": ("credential", "artifact", "vulnerability"),
    }.get(body.phase, ())
    normalized = re.sub(r"\s+", " ", command).strip()
    if _is_axiom_tool_discovery(normalized):
        return GuardDecision(
            decision="ALLOW",
            reason="AXIOM CLI discovery",
            source="whitelist",
        )
    if any(f"axiom-tool {prefix}" in normalized for prefix in allowed):
        return GuardDecision(decision="ALLOW", reason="AXIOM phase permission", source="whitelist")
    return GuardDecision(
        decision="BLOCK",
        reason=f"axiom-tool command is not allowed during {body.phase or 'unknown'}",
        source="hard_rule",
    )


def _is_axiom_tool_discovery(command: str) -> bool:
    """Allow side-effect-free discovery without granting another phase's commands."""
    redirect = r"(?:\s+2>&1)?"
    lookup = rf"(?:which|type)\s+axiom-tool{redirect}"
    command_v = rf"command\s+-v\s+axiom-tool{redirect}"
    root_help = rf"(?:[^\s;|&]*/)?axiom-tool\s+--help{redirect}"
    segment = rf"(?:{lookup}|{command_v}|{root_help})"
    return re.fullmatch(rf"{segment}(?:\s*;\s*{segment})*", command) is not None


def _is_external_operation(tool_name: str, payload: Any) -> bool:
    name = tool_name.strip().lower()
    if name in {"browser", "browser-control", "mcp"} or name.startswith("mcp_"):
        return True
    if name != "bash":
        return False
    command = str(payload.get("command") or payload.get("cmd") or "") if isinstance(payload, dict) else str(payload)
    return bool(
        re.search(
            r"\b(curl|wget|httpx|nmap|subfinder|dirsearch|sqlmap|nuclei|ssh|ftp|nc|netcat|openssl\s+s_client)\b|https?://",
            command,
            re.I,
        )
    )
