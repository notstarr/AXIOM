from __future__ import annotations

from sqlalchemy import Engine, func, select

from axiom.modules.audit.service import AuditService
from axiom.modules.dashboard.schemas import DashboardSummary
from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.modules.token_usage.service import TokenUsageService
from axiom.modules.vulnerabilities.tables import vulnerabilities


class DashboardService:
    def __init__(self, engine: Engine, *, token_usage: TokenUsageService | None = None):
        self.engine = engine
        self.audit = AuditService(engine)
        self.token_usage = token_usage or TokenUsageService(engine)

    def summary(self, *, token_range: str = "all", model: str = "all") -> DashboardSummary:
        with self.engine.connect() as conn:
            task_status_counts = {
                str(row.status): int(row.count)
                for row in conn.execute(
                    select(tasks.c.status, func.count().label("count")).group_by(tasks.c.status)
                )
            }
            vulnerability_severity_counts = {
                str(row.severity): int(row.count)
                for row in conn.execute(
                    select(vulnerabilities.c.severity, func.count().label("count"))
                    .where(vulnerabilities.c.archived.is_(False))
                    .group_by(vulnerabilities.c.severity)
                )
            }
            intent_counts = {
                str(row.status): int(row.count)
                for row in conn.execute(
                    select(intents.c.status, func.count().label("count"))
                    .where(intents.c.archived.is_(False), intents.c.kind == "explore")
                    .group_by(intents.c.status)
                )
            }
        token_dashboard = self.token_usage.dashboard(token_range=token_range, model=model)
        return DashboardSummary(
            task_status_counts=task_status_counts,
            vulnerability_severity_counts=vulnerability_severity_counts,
            active_intent_count=sum(
                intent_counts.get(status, 0) for status in ("claimed", "running", "concluding")
            ),
            pending_intent_count=intent_counts.get("pending", 0),
            token_usage=token_dashboard.token_usage,
            token_by_task=token_dashboard.token_by_task,
            token_series=token_dashboard.token_series,
            available_models=token_dashboard.available_models,
            token_range=token_dashboard.token_range,
            token_model=token_dashboard.token_model,
            recent_events=self.audit.list(limit=8),
        )
