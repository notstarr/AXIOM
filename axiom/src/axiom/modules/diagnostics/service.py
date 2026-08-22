from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, select

from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import intents
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.token_usage.service import TokenUsageService
from axiom.modules.workers.service import WorkerService


PHASES = ("bootstrap", "explore", "reason", "plan", "review")
REASON_LABELS = {
    "timeout": ("执行超时", "high"),
    "lease_expired": ("租约过期", "high"),
    "lease_heartbeat_lost": ("心跳丢失", "high"),
    "worker_infrastructure": ("Worker/API 基础设施失败", "medium"),
    "reason_plan_invalid": ("规划输出无效", "medium"),
}


class DiagnosticsService:
    def __init__(self, engine: Engine, *, token_usage: TokenUsageService | None = None):
        self.engine = engine
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)
        self.settings = RuntimeSettingsService(engine)
        self.workers = WorkerService(engine)
        self.token_usage = token_usage or TokenUsageService(engine)

    def task(
        self,
        task_id: str,
        *,
        token_range: str = "all",
        model: str = "all",
    ) -> dict[str, Any]:
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                from axiom.shared.errors import NotFoundError

                raise NotFoundError(f"Task not found: {task_id}")
            rows = conn.execute(
                select(intents)
                .where(intents.c.task_id == task_id, intents.c.archived.is_(False))
                .order_by(intents.c.created_at)
            ).mappings().all()

        events = self.audit.list(task_id=task_id, limit=2_000)
        runtime = self.settings.get()
        return {
            "task_id": task_id,
            "health": self._health(task, rows, events, runtime.config.lease_timeout_seconds),
            "config_status": {
                "configured_version": runtime.version,
                "applied_version": runtime.applied_version,
                "is_current": runtime.applied_version >= runtime.version,
                "applied_at": runtime.applied_at,
            },
            "worker_rankings": self._worker_rankings(events),
            "token_rankings": self.token_usage.rankings(
                task_id=task_id,
                token_range=token_range,
                model=model,
            ),
            "recent_events": [item.model_dump(mode="json") for item in events[:80]],
        }

    def _health(self, task, rows, events, lease_timeout: int) -> dict[str, Any]:
        active = [row for row in rows if row["status"] in {"claimed", "running", "concluding"}]
        unclaimed = [row for row in rows if row["status"] == "pending"]
        now = datetime.now(timezone.utc)
        stuck = []
        for row in active:
            parsed = _parse_time(row["last_heartbeat_at"] or row["claimed_at"] or row["created_at"])
            if parsed is None:
                continue
            age = max(0, int((now - parsed).total_seconds()))
            timeout = int(row["execute_timeout_seconds"] or lease_timeout)
            if age > max(lease_timeout, timeout):
                stuck.append({
                    "id": row["id"],
                    "worker": row["worker"] or "",
                    "heartbeat_age_seconds": age,
                    "timeout_seconds": timeout,
                    "description": _preview(row["description"]),
                })
        notes: list[str] = []
        available = [worker for worker in self.workers.list() if not worker.disabled]
        if unclaimed and not any(
            row["kind"] in worker.capabilities
            and (not task.worker_pool or worker.name in task.worker_pool)
            for row in unclaimed
            for worker in available
        ):
            notes.append("存在未领取 Intent，但当前没有兼容且可用的 Worker")
        if stuck:
            notes.append("检测到超过租约/执行预算仍未更新心跳的 Intent")
        return {
            "status": "danger" if stuck else ("warning" if notes else "ok"),
            "latest_phase": str(events[0].payload.get("phase") or "") if events else "",
            "running_intent_count": len(active),
            "unclaimed_intent_count": len(unclaimed),
            "stuck_intents": stuck,
            "notes": notes,
        }

    def _worker_rankings(self, events) -> dict[str, Any]:
        boards = {
            phase: {
                "task_type": phase,
                "total": 0,
                "success": 0,
                "failed": 0,
                "timed_out": 0,
                "other": 0,
                "workers": [],
                "reasons": [],
            }
            for phase in PHASES
        }
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        reasons: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for event in events:
            payload = event.payload or {}
            phase = str(payload.get("phase") or "")
            if phase not in boards:
                continue
            if event.event_type in {
                "intent.completed",
                "reason.committed",
                "plan.workspace_committed",
                "plan.committed",
                "review.committed",
            }:
                outcome = "success"
            elif event.event_type in {
                "intent.released",
                "intent.lease_expired",
                "plan.released",
                "plan.lease_expired",
                "plan.workspace_abandoned",
                "review.released",
                "review.lease_expired",
            }:
                failure = str(payload.get("failure_kind") or event.event_type).lower()
                outcome = "timed_out" if any(
                    token in failure for token in ("timeout", "timed_out", "lease", "heartbeat")
                ) else "failed"
            else:
                continue
            worker = event.worker or "未标记 Worker"
            bucket = buckets.setdefault(
                (phase, worker),
                {"worker": worker, "total": 0, "success": 0, "failed": 0, "timed_out": 0, "other": 0, "reasons": []},
            )
            bucket["total"] += 1
            bucket[outcome] += 1
            boards[phase]["total"] += 1
            boards[phase][outcome] += 1
            if outcome != "success":
                code = str(payload.get("failure_kind") or event.event_type)
                label, severity = REASON_LABELS.get(code, (code, "low"))
                reason = reasons[phase].setdefault(
                    code, {"code": code, "label": label, "severity": severity, "count": 0}
                )
                reason["count"] += 1
        for phase in PHASES:
            workers = []
            for (item_phase, _), bucket in buckets.items():
                if item_phase != phase:
                    continue
                total = bucket["total"]
                bucket["completion_rate"] = bucket["success"] / total if total else 0
                bucket["failure_timeout_rate"] = (bucket["failed"] + bucket["timed_out"]) / total if total else 0
                bucket["rank_score"] = 1 - bucket["completion_rate"]
                workers.append(bucket)
            boards[phase]["workers"] = sorted(workers, key=lambda item: (-item["completion_rate"], item["worker"]))
            boards[phase]["reasons"] = sorted(reasons[phase].values(), key=lambda item: (-item["count"], item["code"]))
        return {"boards": boards, "notes": ["统计基于已持久化的 Intent 生命周期事件；旧数据没有事件时不会臆造执行结果。"]}

def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _preview(value: str, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit]
