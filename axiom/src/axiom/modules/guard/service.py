from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.guard.messages import platform_block
from axiom.modules.guard.policy import evaluate, normalize
from axiom.modules.guard.schemas import GuardDecision, GuardRequest
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.repository import TaskRepository


class GuardService:
    def __init__(self, engine: Engine, settings: RuntimeSettingsService):
        self.engine = engine
        self.settings = settings
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)

    def decide(self, request: GuardRequest) -> GuardDecision:
        enabled = False
        engagement_mode = ""
        if request.task_id:
            with self.engine.connect() as conn:
                task = self.tasks.get(conn, request.task_id)
            enabled = bool(task and task.guard_enabled)
            engagement_mode = getattr(task, "engagement_mode", "") or ""
        if not enabled:
            normalized = normalize(request.tool_name, request.input)
            decision = GuardDecision(decision="ALLOW", reason="Task 未启用 Guard", source="disabled")
        else:
            config = self.settings.get().config
            observed = evaluate(request, config, engagement_mode)
            normalized = normalize(request.tool_name, request.input)
            if config.guard_mode == "audit" and observed.decision == "BLOCK":
                decision = GuardDecision(
                    decision="ALLOW",
                    reason="Audit 模式仅记录，不阻断",
                    source=observed.source,
                    observed_decision="BLOCK",
                    observed_reason=observed.reason,
                )
            else:
                decision = observed
        decision = _with_model_message(decision)
        # Only Explore is audited.  Bootstrap, Reason, Plan and Review still get
        # judged - the decision is returned to the worker either way - but their
        # tool calls are not what the operator reviews, and recording them turns
        # the log into noise that hides the Explore rows.
        if (request.phase or "").strip().lower() != "explore":
            return decision
        event = self.audit.record(
            RecordAuditEvent(
                project_id=request.project_id,
                task_id=request.task_id,
                intent_id=request.intent_id,
                reason_run_id=request.reason_run_id,
                plan_run_id=request.plan_run_id,
                review_run_id=request.review_run_id,
                worker=request.worker,
                event_type="guard.decision",
                level="warning" if decision.observed_decision == "BLOCK" or decision.decision == "BLOCK" else "debug",
                message=f"Guard {decision.decision}: {request.tool_name}",
                payload={
                    "phase": request.phase,
                    "engagement_mode": engagement_mode,
                    "tool_call_id": request.tool_call_id,
                    "decision": decision.decision,
                    "reason": decision.reason,
                    # The exact text the Worker received.  Stored alongside the
                    # raw reason so "what the operator sees" and "what the agent
                    # was told" stay distinguishable after the fact.
                    "message": decision.message,
                    "source": decision.source,
                    "observed_decision": decision.observed_decision,
                    "observed_reason": decision.observed_reason,
                    # The audit page shows what was judged, not just the verdict.
                    # Without these it rendered an empty object for every row.
                    "tool_name": normalized["tool_name"],
                    "command": normalized["command"],
                    "normalized": normalized,
                    "input": request.input if isinstance(request.input, dict) else {"value": request.input},
                    "cwd": request.cwd,
                    "model_latency_ms": decision.model_latency_ms,
                    "raw_model_output": decision.raw_model_output,
                    # ``command`` above is the raw text, kept because it is what
                    # the operator investigates with.  ``judge_input`` is the
                    # masked text that was actually sent to the judge, kept so
                    # that "nothing left the host in the clear" is verifiable
                    # from the page rather than from an assertion.
                    "judge_input": decision.judge_input,
                    "redacted_count": decision.redacted_count,
                },
            )
        )
        return decision.model_copy(update={"event_id": event.id})


def _with_model_message(decision: GuardDecision) -> GuardDecision:
    """Attach the model-facing block message.

    Only real verdicts get the platform framing.  ``disabled`` and ``whitelist``
    allow-lists are not decisions about the operation, and a fail-open ALLOW is
    the absence of a decision, so there is nothing to communicate for any of
    them.  The audit-facing ``reason`` is never rewritten.
    """

    if decision.decision != "BLOCK" or decision.source not in {"hard_rule", "model"}:
        return decision
    return decision.model_copy(update={"message": platform_block(decision.reason)})
