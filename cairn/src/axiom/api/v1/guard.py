from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.guard.policy import GuardPolicyConfig, evaluate_guard_event
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.settings.service import RuntimeSettingsService


router = APIRouter(
    prefix="/internal/guard",
    tags=["v1-internal-guard"],
    dependencies=[Depends(require_service_token)],
)


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


@router.post("/pre", response_model=GuardDecision)
def guard_pre(body: GuardRequest, engine=Depends(get_v2_engine)) -> GuardDecision:
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
    if any(f"axiom-tool {prefix}" in normalized for prefix in allowed):
        return GuardDecision(decision="ALLOW", reason="AXIOM phase permission", source="whitelist")
    return GuardDecision(
        decision="BLOCK",
        reason=f"axiom-tool command is not allowed during {body.phase or 'unknown'}",
        source="hard_rule",
    )


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
