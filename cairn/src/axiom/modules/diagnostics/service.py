from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, select

from axiom.infrastructure.database.metadata import (
    audit_events,
    dispatch_workers,
    intents,
    runtime_settings,
    tasks,
)
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.repository import TaskRepository


PHASES = ("bootstrap", "explore", "reason")
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


class TaskDiagnosticsService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.tasks = TaskRepository()

    def get(self, task_id: str) -> dict[str, Any]:
        runtime = RuntimeSettingsService(self.engine).get()
        with self.engine.connect() as conn:
            task = self.tasks.get(conn, task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            intent_rows = list(
                conn.execute(
                    select(intents)
                    .where(intents.c.task_id == task_id)
                    .order_by(intents.c.created_at, intents.c.id)
                ).mappings()
            )
            event_rows = list(
                conn.execute(
                    select(audit_events)
                    .where(audit_events.c.task_id == task_id)
                    .order_by(audit_events.c.created_at.desc(), audit_events.c.id.desc())
                    .limit(2000)
                ).mappings()
            )
            worker_rows = list(
                conn.execute(
                    select(dispatch_workers).order_by(
                        dispatch_workers.c.priority, dispatch_workers.c.name
                    )
                ).mappings()
            )
            settings_row = conn.execute(
                select(runtime_settings).where(
                    runtime_settings.c.id == RuntimeSettingsService.SETTINGS_ID
                )
            ).mappings().one_or_none()

        active = [row for row in intent_rows if row["status"] in {"pending", "claimed", "running", "concluding"}]
        unclaimed = [row for row in active if not row["worker"]]
        running = [row for row in active if row["worker"]]
        stuck = self._stuck_intents(running, runtime.config.lease_timeout_seconds)
        latest = event_rows[0] if event_rows else None
        health_status = "danger" if stuck else "ok"
        health_notes = ["存在疑似超时的执行中意图"] if stuck else []

        return {
            "task": {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "phase": task.phase,
                "worker_pool": task.worker_pool,
                "reason_worker": task.reason_worker,
                "reason_last_heartbeat_age_seconds": self._seconds_since(task.reason_heartbeat_at),
            },
            "health": {
                "status": health_status,
                "notes": health_notes,
                "latest_phase": self._event_phase(latest) if latest else "",
                "latest_event_age_seconds": self._seconds_since(latest["created_at"]) if latest else None,
                "phase_counts": self._phase_counts(event_rows),
                "active_intent_count": len(active),
                "unclaimed_intent_count": len(unclaimed),
                "running_intent_count": len(running),
                "stuck_intents": stuck,
            },
            "config_status": {
                "configured_version": runtime.version,
                "configured_at": runtime.updated_at,
                "applied_version": runtime.version,
                "applied_at": runtime.updated_at,
                "is_current": True,
                "runtime_worker_names": [row["name"] for row in worker_rows if not row["disabled"]],
            },
            "workers": [self._worker(row) for row in worker_rows],
            "worker_rankings": self._worker_rankings(event_rows),
            "token_rankings": self._token_rankings(event_rows),
            "blockers": [],
            "latest_events": [self._event(row) for row in event_rows[:20]],
            "settings_updated_by": settings_row["updated_by"] if settings_row else runtime.updated_by,
        }

    @staticmethod
    def _stuck_intents(rows: list[Any], lease_timeout: int) -> list[dict[str, Any]]:
        items = []
        for row in rows:
            age = TaskDiagnosticsService._seconds_since(row["last_heartbeat_at"] or row["created_at"])
            timeout = int(row["execute_timeout_seconds"] or lease_timeout)
            if age is not None and age > timeout:
                items.append(
                    {
                        "id": row["id"],
                        "worker": row["worker"],
                        "heartbeat_age_seconds": age,
                        "timeout_seconds": timeout,
                        "description": str(row["description"] or "")[:180],
                    }
                )
        return items

    @staticmethod
    def _event_phase(row: Any) -> str:
        payload = TaskDiagnosticsService._payload(row)
        phase = str(payload.get("phase") or "").strip()
        if phase in PHASES:
            return phase
        event_type = str(row["event_type"] or "")
        for candidate in PHASES:
            if event_type.startswith(candidate + ".") or event_type.startswith(candidate + "_"):
                return candidate
        if row["intent_id"] == "i001":
            return "bootstrap"
        if row["intent_id"]:
            return "explore"
        return ""

    @staticmethod
    def _event_outcome(row: Any) -> str:
        event_type = str(row["event_type"] or "")
        payload = TaskDiagnosticsService._payload(row)
        if event_type in {"bootstrap.completed", "explore.completed", "reason.committed"}:
            return "success"
        if event_type == "reason_workspace.abandoned" and str(
            payload.get("failure_reason") or ""
        ).strip() == "Task completed because Goal was achieved":
            return ""
        if event_type in {"intent.released", "intent.retry_exhausted", "reason_workspace.abandoned"}:
            text = " ".join(
                [event_type, str(row["message"] or ""), json.dumps(payload, ensure_ascii=False)]
            ).lower()
            return "timed_out" if "timeout" in text else "failed"
        return ""

    @classmethod
    def _worker_rankings(cls, rows: list[Any]) -> dict[str, Any]:
        boards = {phase: cls._empty_board(phase) for phase in PHASES}
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            phase = cls._event_phase(row)
            outcome = cls._event_outcome(row)
            if phase not in boards or not outcome:
                continue
            worker = str(row["worker"] or "未标记 worker")
            bucket = buckets.setdefault(
                (phase, worker),
                {
                    "worker": worker,
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "timed_out": 0,
                    "other": 0,
                    "latest_at": "",
                    "reasons": {},
                },
            )
            bucket["total"] += 1
            bucket[outcome] += 1
            bucket["latest_at"] = max(bucket["latest_at"], str(row["created_at"] or ""))
            if outcome in {"failed", "timed_out"}:
                code = "timeout" if outcome == "timed_out" else str(cls._payload(row).get("failure_kind") or "failed")
                label = "执行超时/被杀死" if outcome == "timed_out" else code
                reason = bucket["reasons"].setdefault(
                    code,
                    {"code": code, "label": label, "severity": "high" if outcome == "timed_out" else "medium", "count": 0, "examples": []},
                )
                reason["count"] += 1
                if len(reason["examples"]) < 3:
                    reason["examples"].append(str(row["message"] or "")[:220])

        for (phase, _worker), bucket in buckets.items():
            bucket["completion_rate"] = round(bucket["success"] / bucket["total"], 4)
            bucket["failure_timeout_rate"] = round(
                (bucket["failed"] + bucket["timed_out"]) / bucket["total"], 4
            )
            bucket["rank_score"] = bucket["failed"] + bucket["timed_out"]
            bucket["reasons"] = sorted(
                bucket["reasons"].values(), key=lambda item: (-item["count"], item["label"])
            )
            boards[phase]["workers"].append(bucket)

        for board in boards.values():
            board["workers"].sort(
                key=lambda item: (-item["completion_rate"], item["rank_score"], -item["total"], item["worker"])
            )
            for field in ("total", "success", "failed", "timed_out", "other"):
                board[field] = sum(item[field] for item in board["workers"])
            reasons: dict[str, dict[str, Any]] = {}
            for worker in board["workers"]:
                for reason in worker["reasons"]:
                    aggregate = reasons.setdefault(
                        reason["code"],
                        {**reason, "count": 0, "workers": [], "examples": []},
                    )
                    aggregate["count"] += reason["count"]
                    if worker["worker"] not in aggregate["workers"]:
                        aggregate["workers"].append(worker["worker"])
                    aggregate["examples"] = (aggregate["examples"] + reason["examples"])[:3]
            board["reasons"] = sorted(reasons.values(), key=lambda item: (-item["count"], item["label"]))
        return {"phases": list(PHASES), "boards": boards}

    @classmethod
    def _token_rankings(cls, rows: list[Any]) -> dict[str, Any]:
        totals = cls._token_bucket("total")
        workers: dict[str, dict[str, Any]] = {}
        phases = {phase: {**cls._token_bucket(phase), "workers": set()} for phase in PHASES}
        included = 0
        for row in rows:
            usage = cls._normalized_usage(cls._payload(row).get("token_usage"))
            if usage is None:
                continue
            phase = cls._event_phase(row) or "other"
            worker = str(row["worker"] or "未标记 worker")
            worker_bucket = workers.setdefault(worker, {**cls._token_bucket(worker), "phases": set()})
            phase_bucket = phases.setdefault(phase, {**cls._token_bucket(phase), "workers": set()})
            worker_bucket["phases"].add(phase)
            phase_bucket["workers"].add(worker)
            for bucket in (totals, worker_bucket, phase_bucket):
                cls._add_usage(bucket, usage, str(row["created_at"] or ""))
            included += 1

        worker_rows = []
        for worker, bucket in workers.items():
            bucket["worker"] = worker
            bucket["phases"] = sorted(bucket["phases"])
            worker_rows.append(cls._finish_bucket(bucket))
        phase_rows = []
        for phase in PHASES:
            bucket = phases[phase]
            bucket["phase"] = phase
            bucket["workers"] = sorted(bucket["workers"])
            bucket["worker_count"] = len(bucket["workers"])
            phase_rows.append(cls._finish_bucket(bucket))
        return {
            "totals": cls._finish_bucket(totals),
            "by_worker": sorted(worker_rows, key=lambda item: (-item["total_tokens"], item["worker"])),
            "by_phase": sorted(phase_rows, key=lambda item: (-item["total_tokens"], item["phase"])),
            "included_events": included,
            "ignored_possible_duplicates": 0,
            "notes": ["Backend V2 按每次阶段完成事件中的 token_usage 汇总。"],
        }

    @staticmethod
    def _empty_board(phase: str) -> dict[str, Any]:
        return {"task_type": phase, "total": 0, "success": 0, "failed": 0, "timed_out": 0, "other": 0, "workers": [], "reasons": []}

    @staticmethod
    def _token_bucket(name: str) -> dict[str, Any]:
        return {"name": name, "calls": 0, "latest_at": "", **{field: 0 for field in TOKEN_FIELDS}}

    @staticmethod
    def _add_usage(bucket: dict[str, Any], usage: dict[str, int], created_at: str) -> None:
        bucket["calls"] += 1
        for field in TOKEN_FIELDS:
            bucket[field] += int(usage.get(field, 0) or 0)
        bucket["latest_at"] = max(bucket["latest_at"], created_at)

    @staticmethod
    def _finish_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        bucket["avg_total_tokens"] = round(bucket["total_tokens"] / bucket["calls"], 2) if bucket["calls"] else 0
        bucket["active_breakdown_tokens"] = sum(bucket[field] for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        return bucket

    @staticmethod
    def _normalized_usage(raw: Any) -> dict[str, int] | None:
        if not isinstance(raw, dict):
            return None
        usage = {field: max(0, int(raw.get(field, 0) or 0)) for field in TOKEN_FIELDS}
        if not any(usage.values()):
            return None
        if usage["total_tokens"] <= 0:
            usage["total_tokens"] = sum(usage[field] for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        return usage

    @staticmethod
    def _worker(row: Any) -> dict[str, Any]:
        config = json.loads(row["config_json"] or "{}")
        env = config.get("env") if isinstance(config.get("env"), dict) else config
        return {
            "name": row["name"],
            "worker_type": row["worker_type"],
            "task_types": json.loads(row["capabilities_json"] or "[]"),
            "max_running": row["max_running"],
            "priority": row["priority"],
            "disabled": bool(row["disabled"]),
            "model": env.get("PI_MODEL", ""),
            "base_url": env.get("PI_BASE_URL", ""),
            "provider_api": env.get("PI_PROVIDER_API", ""),
            "context_window": env.get("PI_MODEL_CONTEXT_WINDOW", ""),
            "api_key_masked": "***" if env.get("PI_API_KEY") else "",
            "mcp_enabled": str(env.get("PI_MCP_ENABLED", "")).lower() in {"1", "true", "yes", "on"},
            "mcp_profile": env.get("PI_MCP_PROFILE", ""),
            "mcp_servers": [],
        }

    @classmethod
    def _phase_counts(cls, rows: list[Any]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            phase = cls._event_phase(row)
            if phase:
                counts[phase] = counts.get(phase, 0) + 1
        return counts

    @classmethod
    def _event(cls, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_type": cls._event_phase(row),
            "phase": cls._event_phase(row),
            "worker": row["worker"],
            "level": row["level"],
            "message": row["message"],
            "payload": cls._payload(row),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _payload(row: Any) -> dict[str, Any]:
        try:
            value = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _seconds_since(value: str | None) -> int | None:
        if not value:
            return None
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
