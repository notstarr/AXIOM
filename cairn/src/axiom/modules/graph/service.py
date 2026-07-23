from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, delete, select, update

from axiom.infrastructure.database.metadata import (
    artifacts,
    facts,
    intent_sources,
    intents,
    tasks,
    vulnerabilities,
)
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.common.time import utcnow
from axiom.modules.graph.schemas import ConcludeResult, Fact, Intent
from axiom.modules.tasks.repository import TaskRepository


ACTIVE_INTENT_STATUSES = {"claimed", "running", "concluding"}
FAILURE_CLASSES = {"transient", "execution", "planning"}
REQUIRED_ARTIFACTS = {
    "bootstrap": {"asset_inventory", "initial_leads", "findings"},
    "explore": {"findings", "leads"},
}
CONCLUSION_SOURCES = {
    "bootstrap_artifact_summary",
    "explore_execute_summary",
    "explore_conclude_summary",
    "explore_artifact_summary",
    "legacy",
}


class IntentService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)

    def get(self, task_id: str, intent_id: str) -> Intent:
        with self.engine.connect() as conn:
            return self._require_intent(conn, task_id, intent_id)

    def list(self, task_id: str, *, include_archived: bool = False) -> list[Intent]:
        with self.engine.connect() as conn:
            statement = select(intents).where(intents.c.task_id == task_id)
            if not include_archived:
                statement = statement.where(intents.c.archived.is_(False))
            rows = conn.execute(statement.order_by(intents.c.created_at, intents.c.id)).mappings()
            return [self._to_model(conn, row) for row in rows]

    def claim(self, task_id: str, intent_id: str, *, worker: str) -> Intent:
        worker = self._worker(worker)
        now = utcnow()
        with self.engine.begin() as conn:
            task = self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            if intent.status in ACTIVE_INTENT_STATUSES:
                if intent.worker != worker:
                    raise ValueError(f"Intent is currently claimed by {intent.worker}")
                return intent
            if intent.status != "pending":
                raise ValueError(f"Intent is not claimable: {intent.status}")
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(
                    status="claimed",
                    worker=worker,
                    last_heartbeat_at=now,
                    next_retry_at=None,
                    activity_kind="starting",
                    activity_summary="Worker 正在读取任务上下文",
                    activity_tool="",
                    activity_started_at=now,
                    activity_updated_at=now,
                )
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker,
                    event_type="intent.claimed",
                    message=f"Intent {intent_id} claimed by {worker}",
                ),
            )
            return self._require_intent(conn, task_id, intent_id)

    def report_activity(
        self,
        task_id: str,
        intent_id: str,
        *,
        worker: str,
        kind: str,
        summary: str,
        tool: str = "",
    ) -> Intent:
        worker = self._worker(worker)
        kind = " ".join(kind.split())[:64]
        summary = " ".join(summary.split())[:240]
        tool = " ".join(tool.split())[:120]
        if not kind:
            raise ValueError("activity kind must not be empty")
        if not summary:
            raise ValueError("activity summary must not be empty")
        with self.engine.begin() as conn:
            self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            now = utcnow()
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(
                    activity_kind=kind,
                    activity_summary=summary,
                    activity_tool=tool,
                    activity_started_at=intent.activity_started_at or now,
                    activity_updated_at=now,
                )
            )
            return self._require_intent(conn, task_id, intent_id)

    def heartbeat(self, task_id: str, intent_id: str, *, worker: str) -> Intent:
        worker = self._worker(worker)
        with self.engine.begin() as conn:
            self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(status="running", last_heartbeat_at=utcnow())
            )
            return self._require_intent(conn, task_id, intent_id)

    def release(
        self,
        task_id: str,
        intent_id: str,
        *,
        worker: str,
        failure_kind: str = "",
        failure_class: str = "execution",
        next_retry_at: str | None = None,
    ) -> Intent:
        worker = self._worker(worker)
        failure_class = failure_class.strip().lower() or "execution"
        if failure_class not in FAILURE_CLASSES:
            raise ValueError(f"unsupported failure class: {failure_class}")
        with self.engine.begin() as conn:
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            task = self.tasks.get(conn, task_id)
            values = {
                "status": "pending",
                "worker": None,
                "last_heartbeat_at": None,
                "next_retry_at": next_retry_at,
                "activity_kind": "waiting_retry",
                "activity_summary": "执行已中断，等待重新派发",
                "activity_tool": "",
                "activity_updated_at": utcnow(),
            }
            if failure_kind:
                if failure_class == "transient":
                    values["infrastructure_retry_count"] = intent.infrastructure_retry_count + 1
                else:
                    values["retry_count"] = intent.retry_count + 1
                values["last_failure_kind"] = failure_kind
                values["failure_class"] = failure_class
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(**values)
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id if task else None,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker,
                    event_type="intent.released",
                    level="warning" if failure_kind else "info",
                    message=f"Intent {intent_id} released",
                    payload={
                        "failure_kind": failure_kind,
                        "failure_class": failure_class,
                        "next_retry_at": next_retry_at,
                    },
                ),
            )
            return self._require_intent(conn, task_id, intent_id)

    def conclude(
        self,
        task_id: str,
        intent_id: str,
        *,
        worker: str,
        description: str,
        summary_source: str = "legacy",
    ) -> ConcludeResult:
        worker = self._worker(worker)
        description = description.strip()
        if not description:
            raise ValueError("description must not be empty")
        summary_source = summary_source.strip()
        if summary_source not in CONCLUSION_SOURCES:
            raise ValueError(f"unsupported conclusion source: {summary_source}")
        now = utcnow()
        with self.engine.begin() as conn:
            task = self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            self._require_owner(intent, worker)
            phase = "bootstrap" if intent_id == "i001" else "explore"
            artifact_rows = list(
                conn.execute(
                    select(artifacts)
                    .where(
                        artifacts.c.task_id == task_id,
                        artifacts.c.intent_id == intent_id,
                        artifacts.c.archived.is_(False),
                    )
                    .order_by(artifacts.c.created_at, artifacts.c.id)
                ).mappings()
            )
            artifact_types = {row["artifact_type"] for row in artifact_rows}
            missing = sorted(REQUIRED_ARTIFACTS[phase] - artifact_types)
            if missing:
                raise ValueError(
                    f"Intent cannot conclude before required artifacts are registered: {', '.join(missing)}"
                )
            if phase == "bootstrap":
                description = self._bootstrap_fact_description(artifact_rows)
            fact_id = allocate_readable_id(
                conn, scope=task_id, kind="fact", prefix="f"
            )
            conn.execute(
                facts.insert().values(
                    id=fact_id,
                    task_id=task_id,
                    description=description,
                    created_at=now,
                )
            )
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(
                    to_fact_id=fact_id,
                    status="completed",
                    last_heartbeat_at=now,
                    activity_kind="finished",
                    activity_summary=intent.activity_summary or "执行已结束",
                    activity_tool=intent.activity_tool,
                    activity_updated_at=now,
                    concluded_at=now,
                    next_retry_at=None,
                )
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    phase=self._phase_after_intent(conn, task_id, intent_id),
                    updated_at=now,
                )
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    intent_id=intent_id,
                    worker=worker,
                    event_type="intent.completed",
                    message=f"Intent {intent_id} concluded into {fact_id}",
                    payload={"fact_id": fact_id, "summary_source": summary_source},
                ),
            )
            return ConcludeResult(
                fact=Fact(
                    id=fact_id,
                    task_id=task_id,
                    description=description,
                    created_at=now,
                ),
                intent=self._require_intent(conn, task_id, intent_id),
            )

    @classmethod
    def _bootstrap_fact_description(cls, artifact_rows) -> str:
        by_type = {row["artifact_type"]: row for row in artifact_rows}
        labels = {
            "asset_inventory": "攻击面覆盖",
            "initial_leads": "重要线索",
            "findings": "确认结果",
        }
        lines = ["Bootstrap 已根据注册产物完成收口。"]
        index: list[str] = []
        for artifact_type in ("asset_inventory", "initial_leads", "findings"):
            row = by_type[artifact_type]
            preview = cls._artifact_preview(
                Path(row["path"]), fallback=row["summary"] or artifact_type
            )
            lines.append(f"{labels[artifact_type]} [{row['id']}]: {preview}")
            index.append(f"{row['id']}={artifact_type}")
        lines.append(f"产物索引：{', '.join(index)}")
        return "\n".join(lines)

    @staticmethod
    def _artifact_preview(path: Path, *, fallback: str, limit: int = 420) -> str:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:131072]
        except OSError:
            content = fallback
        fragments: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("```"):
                continue
            line = line.lstrip("#>*-+ ").strip()
            if not line or set(line) <= {"-", "|", ":", " "}:
                continue
            fragments.append(" ".join(line.split()))
            if len("; ".join(fragments)) >= limit:
                break
        preview = "; ".join(fragments).strip() or fallback.strip()
        if len(preview) > limit:
            preview = preview[: limit - 1].rstrip() + "…"
        return preview

    def needs_replan(self, task_id: str, intent_id: str, *, reason: str) -> Intent:
        reason = reason.strip() or "retry budget exhausted"
        with self.engine.begin() as conn:
            task = self._require_running_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            if intent.status != "pending":
                raise ValueError(f"Intent cannot be marked for replan: {intent.status}")
            now = utcnow()
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(
                    status="failed",
                    worker=None,
                    last_heartbeat_at=None,
                    activity_kind="failed",
                    activity_summary="执行未完成，等待 Reason 重新规划",
                    activity_tool="",
                    activity_updated_at=now,
                    next_retry_at=None,
                    last_failure_kind="retry_exhausted",
                    failure_class="planning",
                    concluded_at=now,
                )
            )
            conn.execute(
                update(tasks)
                .where(tasks.c.id == task_id)
                .values(
                    phase=self._phase_after_intent(conn, task_id, intent_id),
                    updated_at=now,
                )
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task.project_id,
                    task_id=task_id,
                    intent_id=intent_id,
                    event_type="intent.retry_exhausted",
                    level="warning",
                    message=f"Intent {intent_id} requires replanning",
                    payload={"reason": reason},
                ),
            )
            return self._require_intent(conn, task_id, intent_id)

    @staticmethod
    def _phase_after_intent(conn, task_id: str, current_intent_id: str) -> str:
        remaining = conn.execute(
            select(intents.c.id).where(
                intents.c.task_id == task_id,
                intents.c.id != current_intent_id,
                intents.c.status.in_(["pending", "claimed", "running", "concluding"]),
            ).limit(1)
        ).scalar_one_or_none()
        return "explore" if remaining is not None else "reason"

    def _require_running_task(self, conn, task_id: str):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.legacy_readonly:
            raise ValueError("Legacy task is read-only")
        if task.status != "running":
            raise ValueError("Task is not running")
        return task

    def _require_intent(self, conn, task_id: str, intent_id: str) -> Intent:
        row = conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"Intent not found: {intent_id}")
        return self._to_model(conn, row)

    @staticmethod
    def _require_owner(intent: Intent, worker: str) -> None:
        if intent.status not in ACTIVE_INTENT_STATUSES:
            raise ValueError(f"Intent is not currently claimed: {intent.status}")
        if intent.worker != worker:
            raise ValueError(f"Intent is currently claimed by {intent.worker}")

    @staticmethod
    def _worker(worker: str) -> str:
        value = worker.strip()
        if not value:
            raise ValueError("worker must not be empty")
        return value

    @staticmethod
    def _to_model(conn, row) -> Intent:
        source_rows = conn.execute(
            select(intent_sources.c.fact_id)
            .where(
                intent_sources.c.task_id == row["task_id"],
                intent_sources.c.intent_id == row["id"],
            )
            .order_by(intent_sources.c.fact_id)
        )
        return Intent(
            id=row["id"],
            task_id=row["task_id"],
            todo_id=row["todo_id"],
            from_facts=[item.fact_id for item in source_rows],
            to_fact_id=row["to_fact_id"],
            description=row["description"],
            status=row["status"],
            creator=row["creator"],
            worker=row["worker"],
            worker_pool=json.loads(row["worker_pool_json"] or "[]"),
            targets=json.loads(row["targets_json"] or "[]"),
            execute_timeout_seconds=row["execute_timeout_seconds"],
            conclude_timeout_seconds=row["conclude_timeout_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            activity_kind=row["activity_kind"],
            activity_summary=row["activity_summary"],
            activity_tool=row["activity_tool"],
            activity_started_at=row["activity_started_at"],
            activity_updated_at=row["activity_updated_at"],
            retry_count=row["retry_count"],
            infrastructure_retry_count=row["infrastructure_retry_count"],
            next_retry_at=row["next_retry_at"],
            last_failure_kind=row["last_failure_kind"],
            failure_class=row["failure_class"],
            created_at=row["created_at"],
            concluded_at=row["concluded_at"],
            archived=bool(row["archived"]),
        )


class GraphManagementService:
    """Human graph maintenance kept separate from worker lifecycle commands."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.tasks = TaskRepository()
        self.audit = AuditService(engine)

    def update_fact(self, task_id: str, fact_id: str, *, description: str, actor: str) -> Fact:
        description = self._description(description)
        actor = self._actor(actor)
        with self.engine.begin() as conn:
            task = self._require_mutable_task(conn, task_id)
            self._require_fact(conn, task_id, fact_id)
            conn.execute(
                update(facts)
                .where(facts.c.task_id == task_id, facts.c.id == fact_id)
                .values(description=description)
            )
            self._audit(conn, task, actor, "fact.updated", f"Fact {fact_id} updated", {"fact_id": fact_id})
            return self._require_fact(conn, task_id, fact_id)

    def toggle_fact_archive(self, task_id: str, fact_id: str, *, actor: str) -> Fact:
        if fact_id in {"origin", "goal"}:
            raise ValueError(f"Cannot archive protected fact: {fact_id}")
        actor = self._actor(actor)
        with self.engine.begin() as conn:
            task = self._require_mutable_task(conn, task_id)
            fact = self._require_fact(conn, task_id, fact_id)
            archived = not fact.archived
            conn.execute(
                update(facts)
                .where(facts.c.task_id == task_id, facts.c.id == fact_id)
                .values(archived=archived)
            )
            self._audit(
                conn,
                task,
                actor,
                "fact.archived" if archived else "fact.unarchived",
                f"Fact {fact_id} {'archived' if archived else 'unarchived'}",
                {"fact_id": fact_id, "archived": archived},
            )
            return self._require_fact(conn, task_id, fact_id)

    def delete_fact(self, task_id: str, fact_id: str, *, actor: str) -> None:
        if fact_id in {"origin", "goal"}:
            raise ValueError(f"Cannot delete protected fact: {fact_id}")
        actor = self._actor(actor)
        with self.engine.begin() as conn:
            task = self._require_mutable_task(conn, task_id)
            self._require_fact(conn, task_id, fact_id)
            producer_ids = list(
                conn.execute(
                    select(intents.c.id).where(
                        intents.c.task_id == task_id,
                        intents.c.to_fact_id == fact_id,
                    )
                ).scalars()
            )
            conn.execute(
                delete(intent_sources).where(
                    intent_sources.c.task_id == task_id,
                    intent_sources.c.fact_id == fact_id,
                )
            )
            if producer_ids:
                conn.execute(
                    update(intents)
                    .where(intents.c.task_id == task_id, intents.c.id.in_(producer_ids))
                    .values(
                        to_fact_id=None,
                        status="pending",
                        worker=None,
                        last_heartbeat_at=None,
                        activity_kind="",
                        activity_summary="",
                        activity_tool="",
                        activity_started_at=None,
                        activity_updated_at=None,
                        concluded_at=None,
                        retry_count=0,
                        infrastructure_retry_count=0,
                        next_retry_at=None,
                        last_failure_kind="",
                        failure_class="",
                        archived=False,
                    )
                )
            conn.execute(delete(facts).where(facts.c.task_id == task_id, facts.c.id == fact_id))
            if producer_ids:
                phase = "bootstrap" if "i001" in producer_ids else "explore"
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == task_id)
                    .values(phase=phase, updated_at=utcnow())
                )
            self._audit(
                conn,
                task,
                actor,
                "fact.deleted",
                f"Fact {fact_id} deleted",
                {"fact_id": fact_id, "reopened_intent_ids": producer_ids},
            )

    def update_intent(self, task_id: str, intent_id: str, *, description: str, actor: str) -> Intent:
        description = self._description(description)
        actor = self._actor(actor)
        with self.engine.begin() as conn:
            task = self._require_mutable_task(conn, task_id)
            self._require_intent(conn, task_id, intent_id)
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(description=description)
            )
            self._audit(conn, task, actor, "intent.updated", f"Intent {intent_id} updated", {"intent_id": intent_id})
            return self._require_intent(conn, task_id, intent_id)

    def toggle_intent_archive(self, task_id: str, intent_id: str, *, actor: str) -> Intent:
        if intent_id == "i001":
            raise ValueError("Cannot archive Bootstrap intent i001")
        actor = self._actor(actor)
        with self.engine.begin() as conn:
            task = self._require_mutable_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            if intent.status in ACTIVE_INTENT_STATUSES:
                raise ValueError(f"Cannot archive active intent: {intent.status}")
            archived = not intent.archived
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task_id, intents.c.id == intent_id)
                .values(archived=archived)
            )
            self._audit(
                conn,
                task,
                actor,
                "intent.archived" if archived else "intent.unarchived",
                f"Intent {intent_id} {'archived' if archived else 'unarchived'}",
                {"intent_id": intent_id, "archived": archived},
            )
            return self._require_intent(conn, task_id, intent_id)

    def delete_intent(self, task_id: str, intent_id: str, *, actor: str) -> None:
        if intent_id == "i001":
            raise ValueError("Cannot delete Bootstrap intent i001")
        actor = self._actor(actor)
        with self.engine.begin() as conn:
            task = self._require_mutable_task(conn, task_id)
            intent = self._require_intent(conn, task_id, intent_id)
            if intent.status in ACTIVE_INTENT_STATUSES:
                raise ValueError(f"Cannot delete active intent: {intent.status}")
            if intent.to_fact_id:
                raise ValueError("Cannot delete an intent that already produced a fact")
            linked_artifact = conn.execute(
                select(artifacts.c.id).where(
                    artifacts.c.task_id == task_id,
                    artifacts.c.intent_id == intent_id,
                ).limit(1)
            ).scalar_one_or_none()
            linked_vulnerability = conn.execute(
                select(vulnerabilities.c.id).where(
                    vulnerabilities.c.task_id == task_id,
                    vulnerabilities.c.intent_id == intent_id,
                ).limit(1)
            ).scalar_one_or_none()
            if linked_artifact or linked_vulnerability:
                raise ValueError("Cannot delete an intent with linked artifacts or vulnerabilities")
            conn.execute(
                delete(intent_sources).where(
                    intent_sources.c.task_id == task_id,
                    intent_sources.c.intent_id == intent_id,
                )
            )
            conn.execute(delete(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id))
            self._audit(conn, task, actor, "intent.deleted", f"Intent {intent_id} deleted", {"intent_id": intent_id})

    def _require_mutable_task(self, conn, task_id: str):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if task.legacy_readonly:
            raise ValueError("Legacy task is read-only")
        if task.status != "running":
            raise ValueError("Task is not running")
        return task

    @staticmethod
    def _require_fact(conn, task_id: str, fact_id: str) -> Fact:
        row = conn.execute(
            select(facts).where(facts.c.task_id == task_id, facts.c.id == fact_id)
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"Fact not found: {fact_id}")
        return Fact(**row)

    @staticmethod
    def _require_intent(conn, task_id: str, intent_id: str) -> Intent:
        row = conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"Intent not found: {intent_id}")
        return IntentService._to_model(conn, row)

    def _audit(self, conn, task, actor: str, event_type: str, message: str, payload: dict) -> None:
        self.audit.record_in(
            conn,
            RecordAuditEvent(
                project_id=task.project_id,
                task_id=task.id,
                worker=actor,
                event_type=event_type,
                message=message,
                payload=payload,
            ),
        )

    @staticmethod
    def _description(value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("description must not be empty")
        return text

    @staticmethod
    def _actor(value: str) -> str:
        return value.strip() or "human"
