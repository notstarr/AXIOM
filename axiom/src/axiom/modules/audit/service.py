from __future__ import annotations

import json
import uuid

from sqlalchemy import Engine, Text, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

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

    def stats(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        intent_id: str | None = None,
        event_type: str | None = None,
        level: str | None = None,
    ) -> dict:
        """Counts grouped by decision and by source, over the *whole* table.

        The page loads one page at a time, so a counter derived from the loaded
        rows shows "拦截 22" and then climbs as the operator scrolls.  These come
        from SQL so the numbers are the stored truth regardless of paging.

        For guard decisions the verdict and the source live in payload_json; for
        other event types there is no decision, so everything counts as one
        bucket.
        """

        # payload_json goes through a JSONText type that renders as text, so the
        # column does not support subscripting.  Cast to jsonb and use ->> so the
        # grouping runs in Postgres against the stored JSON.
        payload = cast(audit_events.c.payload_json, JSONB)
        decision = payload.op("->>")("decision").label("decision")
        source = payload.op("->>")("source").label("source")

        query = select(
            decision.label("decision"),
            source.label("source"),
            func.count().label("n"),
        ).select_from(audit_events)
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
        query = query.group_by(decision, source)

        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()

        total = 0
        blocked = 0
        by_source: dict[str, int] = {}
        for row in rows:
            n = int(row["n"] or 0)
            total += n
            if str(row["decision"] or "").upper() == "BLOCK":
                blocked += n
            key = str(row["source"] or "")
            if key:
                by_source[key] = by_source.get(key, 0) + n
        return {
            "total": total,
            "blocked": blocked,
            "allowed": total - blocked,
            "rate": round(blocked / total * 100) if total else 0,
            "by_source": by_source,
        }

    def count(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        intent_id: str | None = None,
        event_type: str | None = None,
        level: str | None = None,
    ) -> int:
        """Total rows matching the same filters as :meth:`list`.

        The audit page pages through records, so it needs the real total to show
        "总记录" rather than however much it has scrolled so far.
        """

        query = select(func.count()).select_from(audit_events)
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
        with self.engine.connect() as conn:
            return int(conn.execute(query).scalar_one())

    def list(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        intent_id: str | None = None,
        event_type: str | None = None,
        level: str | None = None,
        decision: str | None = None,
        decision_source: str | None = None,
        search: str = "",
        after_id: str | None = None,
        before_id: str | None = None,
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
        if decision or decision_source:
            # 裁决与依据都只存在于 payload_json 里，必须在 SQL 里筛。以前页面
            # 取回一页之后在内存里过滤，于是「仅拦截」只能看见恰好落在这一页
            # 的那几条——线上的 19 条拦截全部落在第一页之外，页面就显示 0 条。
            payload = cast(audit_events.c.payload_json, JSONB)
            if decision:
                query = query.where(
                    payload.op("->>")("decision") == decision.strip().upper()
                )
            if decision_source:
                query = query.where(
                    payload.op("->>")("source") == decision_source.strip()
                )
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
            if before_id:
                # Cursor for the newest-first list: page backwards through older
                # rows while keeping that same ordering.  ``after_id`` is the
                # forward cursor used by callers that replay in time order.
                cursor = conn.execute(
                    select(audit_events.c.created_at, audit_events.c.id).where(
                        audit_events.c.id == before_id
                    )
                ).mappings().one_or_none()
                if cursor is not None:
                    query = query.where(
                        or_(
                            audit_events.c.created_at < cursor["created_at"],
                            and_(
                                audit_events.c.created_at == cursor["created_at"],
                                audit_events.c.id < cursor["id"],
                            ),
                        )
                    )
                query = query.order_by(audit_events.c.created_at.desc(), audit_events.c.id.desc())
            elif after_id:
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
            rows = conn.execute(query.limit(min(max(limit, 1), 10_000))).mappings()
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
