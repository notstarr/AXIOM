from __future__ import annotations

import json
import uuid

from sqlalchemy import Engine, Text, and_, cast, or_, select

from axiom.modules.audit.schemas import AuditEvent, RecordAuditEvent
from axiom.modules.audit.tables import audit_events
from axiom.shared.clock import utcnow


class AuditService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def record(self, body: RecordAuditEvent) -> AuditEvent:
        with self.engine.begin() as conn:
            return self.record_in(conn, body)

    def record_in(self, conn, body: RecordAuditEvent) -> AuditEvent:
        values = {
            "id": f"evt_{uuid.uuid4().hex}",
            "project_id": body.project_id,
            "task_id": body.task_id,
            "intent_id": body.intent_id,
            "reason_run_id": body.reason_run_id,
            "plan_run_id": body.plan_run_id,
            "review_run_id": body.review_run_id,
            "workspace_id": body.workspace_id,
            "worker": body.worker,
            "event_type": body.event_type,
            "level": body.level,
            "message": body.message,
            "payload_json": json.dumps(body.payload, ensure_ascii=False),
            "created_at": utcnow(),
        }
        conn.execute(audit_events.insert().values(**values))
        return self._model(values)

    def list(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        intent_id: str | None = None,
        event_type: str | None = None,
        level: str | None = None,
        search: str = "",
        after_id: str | None = None,
        limit: int = 500,
    ) -> list[AuditEvent]:
        query = select(audit_events)
        if project_id:
            query = query.where(audit_events.c.project_id == project_id)
        if task_id:
            query = query.where(audit_events.c.task_id == task_id)
        if intent_id:
            query = query.where(audit_events.c.intent_id == intent_id)
        if event_type:
            query = query.where(audit_events.c.event_type == event_type)
        if level:
            query = query.where(audit_events.c.level == level)
        if search.strip():
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    audit_events.c.message.ilike(pattern),
                    audit_events.c.worker.ilike(pattern),
                    cast(audit_events.c.payload_json, Text).ilike(pattern),
                )
            )
        with self.engine.connect() as conn:
            if after_id:
                cursor = conn.execute(
                    select(audit_events.c.created_at, audit_events.c.id).where(
                        audit_events.c.id == after_id
                    )
                ).mappings().one_or_none()
                if cursor is not None:
                    query = query.where(
                        or_(
                            audit_events.c.created_at > cursor["created_at"],
                            and_(
                                audit_events.c.created_at == cursor["created_at"],
                                audit_events.c.id > cursor["id"],
                            ),
                        )
                    )
                query = query.order_by(audit_events.c.created_at, audit_events.c.id)
            else:
                query = query.order_by(audit_events.c.created_at.desc(), audit_events.c.id.desc())
            rows = conn.execute(query.limit(min(max(limit, 1), 2_000))).mappings()
            return [self._model(row) for row in rows]

    @staticmethod
    def _model(row) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            intent_id=row["intent_id"],
            reason_run_id=row.get("reason_run_id"),
            plan_run_id=row.get("plan_run_id"),
            review_run_id=row.get("review_run_id"),
            workspace_id=row["workspace_id"],
            worker=row["worker"],
            event_type=row["event_type"],
            level=row["level"],
            message=row["message"],
            payload=json.loads(row["payload_json"] or "{}"),
            created_at=row["created_at"],
        )
