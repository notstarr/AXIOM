from __future__ import annotations

from sqlalchemy import Engine

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.guard.policy import evaluate
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
        if request.task_id:
            with self.engine.connect() as conn:
                task = self.tasks.get(conn, request.task_id)
            enabled = bool(task and task.guard_enabled)
        if not enabled:
            decision = GuardDecision(decision="ALLOW", reason="Task 未启用 Guard", source="disabled")
        else:
            config = self.settings.get().config
            observed = evaluate(request, config)
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
        event = self.audit.record(
            RecordAuditEvent(
                project_id=request.project_id,
                task_id=request.task_id,
                intent_id=request.intent_id,
                reason_run_id=request.reason_run_id,
                review_run_id=request.review_run_id,
                worker=request.worker,
                event_type="guard.decision",
                level="warning" if decision.observed_decision == "BLOCK" or decision.decision == "BLOCK" else "debug",
                message=f"Guard {decision.decision}: {request.tool_name}",
                payload={
                    "phase": request.phase,
                    "tool_call_id": request.tool_call_id,
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "source": decision.source,
                    "observed_decision": decision.observed_decision,
                    "observed_reason": decision.observed_reason,
                },
            )
        )
        return decision.model_copy(update={"event_id": event.id})
