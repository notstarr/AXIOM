from __future__ import annotations

import json

from sqlalchemy import Engine, func, select

from axiom.modules.audit.service import AuditService
from axiom.modules.audit.tables import audit_events
from axiom.modules.dashboard.schemas import DashboardSummary, TaskTokenUsage, TokenUsageSummary
from axiom.modules.graph.tables import intents
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerabilities.tables import vulnerabilities


class DashboardService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.audit = AuditService(engine)

    def summary(self) -> DashboardSummary:
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
            token_rows = conn.execute(
                select(
                    audit_events.c.task_id,
                    audit_events.c.payload_json,
                    tasks.c.title,
                )
                .join(tasks, tasks.c.id == audit_events.c.task_id)
                .where(audit_events.c.payload_json.like('%"token_usage"%'))
            ).mappings()

            by_task: dict[str, dict[str, int | str]] = {}
            totals = {"input": 0, "output": 0, "total": 0}
            for row in token_rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                usage = payload.get("token_usage")
                if not isinstance(usage, dict):
                    continue
                normalized = _token_usage(usage)
                task_id = str(row["task_id"])
                bucket = by_task.setdefault(
                    task_id,
                    {"task_id": task_id, "title": str(row["title"]), "input": 0, "output": 0, "total": 0},
                )
                for key in ("input", "output", "total"):
                    value = normalized[key]
                    bucket[key] = int(bucket[key]) + value
                    totals[key] += value

        token_by_task = [TaskTokenUsage.model_validate(item) for item in by_task.values()]
        token_by_task.sort(key=lambda item: (-item.total, item.task_id))
        return DashboardSummary(
            task_status_counts=task_status_counts,
            vulnerability_severity_counts=vulnerability_severity_counts,
            active_intent_count=sum(
                intent_counts.get(status, 0) for status in ("claimed", "running", "concluding")
            ),
            pending_intent_count=intent_counts.get("pending", 0),
            token_usage=TokenUsageSummary(**totals),
            token_by_task=token_by_task,
            recent_events=self.audit.list(limit=8),
        )


def _token_usage(raw: dict) -> dict[str, int]:
    input_tokens = _first_int(raw, "input_tokens", "input", "prompt_tokens")
    output_tokens = _first_int(raw, "output_tokens", "output", "completion_tokens")
    total_tokens = _first_int(raw, "total_tokens", "total", "totalTokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return {"input": input_tokens, "output": output_tokens, "total": total_tokens}


def _first_int(raw: dict, *keys: str) -> int:
    for key in keys:
        if key in raw:
            try:
                return max(0, int(raw[key] or 0))
            except (TypeError, ValueError):
                return 0
    return 0
