from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator

from axiom.dispatcher.keyring import load_provider_api_key
from axiom.dispatcher.task_scope import load_task_scope, out_of_scope_host_for_tool
from axiom.guard.policy import GuardPolicyConfig, evaluate_guard_event, new_hook_id, normalize_tool_input
from axiom.server.judge_settings import load_judge_settings
from axiom.server.db import get_conn
from axiom.server.services import utcnow

router = APIRouter(tags=["tool-hooks"])

GuardMode = Literal["off", "observe", "guarded", "enforce"]


class ToolHookPreRequest(BaseModel):
    project_id: str = ""
    intent_id: str = ""
    phase: str = ""
    worker: str = ""
    tool_name: str
    tool_call_id: str = ""
    input: Any = Field(default_factory=dict)
    cwd: str = ""
    mode: GuardMode | None = None

    @field_validator("project_id", "intent_id", "phase", "worker", "tool_name", "tool_call_id", "cwd", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return str(value or "").strip()


class ToolHookPreResponse(BaseModel):
    decision: Literal["ALLOW", "BLOCK"]
    reason: str = ""
    event_id: str
    mode: GuardMode
    observed_decision: Literal["ALLOW", "BLOCK"]
    observed_reason: str = ""
    source: str = ""
    enforced: bool = False


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return json.dumps({"repr": repr(value)}, ensure_ascii=False, separators=(",", ":"))


@router.post("/tool-hooks/pre", response_model=ToolHookPreResponse)
def pre_tool_hook(body: ToolHookPreRequest) -> ToolHookPreResponse:
    event_id = new_hook_id()
    now = utcnow()
    requested_mode = body.mode
    metadata = {
        "project_id": body.project_id,
        "intent_id": body.intent_id,
        "phase": body.phase,
        "worker": body.worker,
        "tool_call_id": body.tool_call_id,
        "cwd": body.cwd,
    }

    with get_conn() as conn:
        judge_settings = load_judge_settings(conn)
    mode = requested_mode or judge_settings.mode
    scope_violation = None
    if body.phase.startswith("bootstrap") or body.phase.startswith("explore"):
        scope_violation = out_of_scope_host_for_tool(
            load_task_scope(body.project_id),
            body.tool_name,
            body.input,
        )

    if scope_violation is not None:
        evaluation = None
        observed_decision = "BLOCK"
        observed_reason = f"任务级 Scope 拦截：{scope_violation}"
        source = "task_scope"
        normalized = normalize_tool_input(body.tool_name, body.input)
        model_name = ""
        latency_ms = 0
        raw_model_output = ""
    elif mode == "off":
        evaluation = None
        observed_decision = "ALLOW"
        observed_reason = ""
        source = "disabled"
        normalized = {}
        model_name = judge_settings.model
        latency_ms = 0
        raw_model_output = ""
    else:
        effective_mode = mode
        evaluation = evaluate_guard_event(
            tool_name=body.tool_name,
            input_payload=body.input,
            metadata=metadata,
            config=GuardPolicyConfig(
                mode=effective_mode,
                base_url=judge_settings.base_url,
                model_name=judge_settings.model,
                timeout_seconds=judge_settings.timeout_seconds,
                whitelist_tools=judge_settings.whitelist_tools,
                whitelist_bash_verbs=judge_settings.whitelist_bash_verbs,
                system_prompt=judge_settings.system_prompt,
                hard_block_rules=judge_settings.hard_block_rules,
            ),
            api_key=load_provider_api_key("judge"),
        )
        observed_decision = evaluation.decision
        observed_reason = evaluation.reason
        source = evaluation.source
        normalized = evaluation.normalized
        model_name = evaluation.model_name
        latency_ms = evaluation.model_latency_ms
        raw_model_output = evaluation.raw_model_output

    decision = "ALLOW"
    reason = ""
    if scope_violation is not None:
        decision = "BLOCK"
        reason = observed_reason
    elif mode == "enforce" and observed_decision == "BLOCK":
        decision = "BLOCK"
        reason = f"🤖裁判模型提醒:{observed_reason}"
    elif mode == "guarded" and observed_decision == "BLOCK" and source == "hard_rule":
        decision = "BLOCK"
        reason = f"🤖裁判模型提醒:{observed_reason}"

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tool_hook_events (
                id, project_id, intent_id, phase, worker, tool_name, tool_call_id,
                input_json, normalized_json, decision, observed_decision,
                decision_source, reason, mode, model_name, model_latency_ms,
                raw_model_output, cwd, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                body.project_id,
                body.intent_id,
                body.phase,
                body.worker,
                body.tool_name,
                body.tool_call_id,
                _json_dumps(body.input),
                _json_dumps(normalized),
                decision,
                observed_decision,
                source,
                reason or observed_reason,
                mode,
                model_name,
                latency_ms,
                raw_model_output[:2000],
                body.cwd,
                now,
            ),
        )

    return ToolHookPreResponse(
        decision=decision,
        reason=reason,
        event_id=event_id,
        mode=mode,
        observed_decision=observed_decision,
        observed_reason=observed_reason,
        source=source,
        enforced=decision == "BLOCK",
    )


@router.get("/tool-hooks/events")
def list_tool_hook_events(
    project_id: str = "",
    decision: Literal["all", "allow", "block"] = "all",
    limit: int = Query(default=100, ge=1, le=1000),
):
    clauses: list[str] = []
    params: list[Any] = []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if decision != "all":
        clauses.append("decision = ?")
        params.append(decision.upper())
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM tool_hook_events{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
