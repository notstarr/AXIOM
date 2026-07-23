from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, select

from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import intents
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.workers.service import WorkerService


PHASES = ("bootstrap", "explore", "reason")
REASON_LABELS = {
    "timeout": ("执行超时", "high"),
    "lease_expired": ("租约过期", "high"),
    "lease_heartbeat_lost": ("心跳丢失", "high"),
    "worker_infrastructure": ("Worker/API 基础设施失败", "medium"),
    "reason_plan_invalid": ("规划输出无效", "medium"),
}


class DiagnosticsService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)
        self.settings = RuntimeSettingsService(engine)
        self.workers = WorkerService(engine)

    def task(self, task_id: str) -> dict[str, Any]:
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
                "applied_version": runtime.version,
                "is_current": True,
                "applied_at": runtime.updated_at,
            },
            "worker_rankings": self._worker_rankings(events),
            "token_rankings": self._token_rankings(events),
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
            if event.event_type in {"intent.completed", "reason.committed"}:
                outcome = "success"
            elif event.event_type in {"intent.released", "intent.lease_expired"}:
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

    def _token_rankings(self, events) -> dict[str, Any]:
        totals = _token_bucket("总计")
        by_worker: dict[str, dict[str, Any]] = {}
        by_phase: dict[str, dict[str, Any]] = {}
        for event in events:
            usage = event.payload.get("token_usage") if isinstance(event.payload, dict) else None
            if not isinstance(usage, dict):
                continue
            phase = str(event.payload.get("phase") or "unknown")
            worker = event.worker or "未标记 Worker"
            worker_row = by_worker.setdefault(worker, _token_bucket(worker))
            phase_row = by_phase.setdefault(phase, _token_bucket(phase))
            _add_usage(totals, usage)
            _add_usage(worker_row, usage)
            _add_usage(phase_row, usage)
            worker_row["phases"].add(phase)
            phase_row["workers"].add(worker)
        return {
            "totals": _finalize_tokens(totals),
            "by_worker": [_finalize_tokens(item) for item in sorted(by_worker.values(), key=lambda x: -x["total_tokens"])],
            "by_phase": [_finalize_tokens(item) for item in sorted(by_phase.values(), key=lambda x: -x["total_tokens"])],
            "notes": ["当前仅统计带 token_usage 的持久化事件；没有该事件时显示为 0，不从文本猜测 token。"],
        }


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


def _token_bucket(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "worker": name,
        "phase": name,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "phases": set(),
        "workers": set(),
    }


def _add_usage(bucket: dict[str, Any], raw: dict[str, Any]) -> None:
    mapping = {
        "input_tokens": ("input_tokens", "input", "prompt_tokens"),
        "output_tokens": ("output_tokens", "output", "completion_tokens"),
        "cache_read_tokens": ("cache_read_tokens", "cacheRead", "cache_read"),
        "cache_write_tokens": ("cache_write_tokens", "cacheWrite", "cache_write"),
        "reasoning_tokens": ("reasoning_tokens", "reasoning", "reasoningTokens"),
        "total_tokens": ("total_tokens", "total", "totalTokens"),
    }
    for destination, keys in mapping.items():
        for key in keys:
            if key in raw:
                try:
                    bucket[destination] += int(raw[key] or 0)
                except (TypeError, ValueError):
                    pass
                break
    bucket["calls"] += 1


def _finalize_tokens(bucket: dict[str, Any]) -> dict[str, Any]:
    result = dict(bucket)
    result["phases"] = sorted(result.get("phases") or [])
    result["workers"] = sorted(result.get("workers") or [])
    return result
