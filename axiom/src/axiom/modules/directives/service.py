"""Human-issued worker directives: CRUD plus the execution-side resolver.

Design boundaries that are easy to break, so they live here in one place:

* Creating a directive **never** triggers a ReasonRun. One-shot ``reason_next``
  rows simply wait until the platform naturally creates its next ReasonRun.
* One-shot delivery is frozen at claim time into ``directive_deliveries``.
  ``/input`` only reads that ledger, so retries of the same lease and re-claims
  with a new lease cannot double-deliver or drop an instruction.
* Persistent instructions are re-read on every execution input; only
  ``all_workers`` reaches conclude (it rides the shared system-prompt prefix).
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, select

from axiom.modules.audit.schemas import RecordAuditEvent
from axiom.modules.audit.service import AuditService
from axiom.modules.directives.repository import DirectiveRepository, _delivery
from axiom.modules.directives.tables import directive_deliveries
from axiom.modules.directives.schemas import (
    CreateDirective,
    Directive,
    EligibleExplore,
    ONCE_SCOPES,
    UpdateDirective,
    WorkerDirectiveBundle,
)
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, NotFoundError


class DirectiveService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = DirectiveRepository()
        self.audit = AuditService(engine)

    # ── inventory ───────────────────────────────────────────────────────────

    def list(self, task_id: str, *, status: str | None = None) -> list[Directive]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return self.repository.list(conn, task_id, status=status)

    def create(self, task_id: str, body: CreateDirective) -> Directive:
        now = utcnow()
        with transaction(self.engine) as conn:
            task = self._require_task(conn, task_id)
            if body.client_message_id:
                existing = self.repository.by_client_id(
                    conn, task_id, body.client_message_id
                )
                if existing is not None:
                    return existing

            if body.scope == "explore_one":
                intent = self.repository.intent_status(
                    conn, task_id, body.target_intent_id or ""
                )
                if (
                    intent is None
                    or intent["kind"] != "explore"
                    or intent["status"] != "pending"
                    or bool(intent["archived"])
                ):
                    raise ConflictError(
                        "该 Explore 不可投递：只允许对尚未领取（pending）的 Explore 下发一次性指令"
                    )

            lifetime = "once" if body.scope in ONCE_SCOPES else "persistent"
            initial_status = "pending" if lifetime == "once" else "active"
            directive = self.repository.create(
                conn,
                task_id=task_id,
                content=body.content.strip(),
                creator=body.creator.strip() or "human",
                scope=body.scope,
                lifetime=lifetime,
                target_intent_id=body.target_intent_id,
                status=initial_status,
                client_message_id=body.client_message_id,
                expires_at=body.expires_at,
                now=now,
            )
            self.audit.record_in(
                conn,
                RecordAuditEvent(
                    project_id=task["project_id"],
                    task_id=task_id,
                    worker=directive.creator,
                    event_type="directive.created",
                    message=f"已下发 {body.scope} 指令",
                    payload={
                        "directive_id": directive.id,
                        "scope": body.scope,
                        "lifetime": lifetime,
                        "target_intent_id": body.target_intent_id,
                    },
                ),
            )
            return directive

    def update(
        self, task_id: str, directive_id: str, body: UpdateDirective
    ) -> Directive:
        now = utcnow()
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            current = self.repository.get(conn, task_id, directive_id)
            if current is None:
                raise NotFoundError(f"Directive not found: {directive_id}")
            values: dict = {}
            if body.content is not None:
                values["content"] = body.content.strip()
            if body.expires_at is not None:
                values["expires_at"] = body.expires_at
            if body.disabled is True:
                values["status"] = "disabled"
                values["disabled_at"] = now
            elif body.disabled is False and current.status == "disabled":
                values["status"] = "active"
                values["disabled_at"] = None
            if values:
                self.repository.update(
                    conn, task_id, directive_id, values, now=now
                )
            updated = self.repository.get(conn, task_id, directive_id)
            assert updated is not None
            return self._with_deliveries(conn, task_id, updated)

    def delete(self, task_id: str, directive_id: str) -> None:
        now = utcnow()
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            current = self.repository.get(conn, task_id, directive_id)
            if current is None:
                raise NotFoundError(f"Directive not found: {directive_id}")
            self.repository.update(
                conn,
                task_id,
                directive_id,
                {"archived": True, "status": "disabled", "disabled_at": now},
                now=now,
            )

    def eligible_explores(self, task_id: str) -> list[EligibleExplore]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return self.repository.eligible_explores(conn, task_id)

    # ── execution-side helpers (called inside the caller's transaction) ─────

    def resolve(
        self,
        conn: Connection,
        *,
        task_id: str,
        phase: str,
        lease_id: str,
        intent_id: str | None = None,
        reason_run_id: str | None = None,
    ) -> WorkerDirectiveBundle:
        return self.repository.resolve(
            conn,
            task_id=task_id,
            phase=phase,
            lease_id=lease_id,
            intent_id=intent_id,
            reason_run_id=reason_run_id,
            now=utcnow(),
        )

    def deliver_for_reason_run(
        self, conn: Connection, *, task_id: str, reason_run_id: str
    ) -> list[dict]:
        return self.repository.consume_for_reason_run(
            conn, task_id=task_id, reason_run_id=reason_run_id, now=utcnow()
        )

    def deliver_for_intent(
        self, conn: Connection, *, task_id: str, intent_id: str, lease_id: str
    ) -> list[dict]:
        return self.repository.consume_for_intent(
            conn, task_id=task_id, intent_id=intent_id, lease_id=lease_id, now=utcnow()
        )

    def mark_delivered(
        self,
        conn: Connection,
        *,
        task_id: str,
        lease_id: str | None = None,
        reason_run_id: str | None = None,
    ) -> None:
        self.repository.mark_delivered(
            conn,
            task_id=task_id,
            lease_id=lease_id,
            reason_run_id=reason_run_id,
            now=utcnow(),
        )

    def reopen_queued(
        self,
        conn: Connection,
        *,
        task_id: str,
        lease_id: str | None = None,
        reason_run_id: str | None = None,
    ) -> list[str]:
        return self.repository.reopen_queued(
            conn,
            task_id=task_id,
            lease_id=lease_id,
            reason_run_id=reason_run_id,
            now=utcnow(),
        )

    # ── internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _require_task(conn: Connection, task_id: str):
        row = conn.execute(
            select(tasks.c.id, tasks.c.project_id, tasks.c.archived, tasks.c.legacy_readonly)
            .where(tasks.c.id == task_id)
        ).mappings().one_or_none()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if row["archived"] or row["legacy_readonly"]:
            raise ConflictError("Archived or read-only Task cannot manage directives")
        return row

    @staticmethod
    def _with_deliveries(
        conn: Connection, task_id: str, directive: Directive
    ) -> Directive:
        directive.deliveries = [
            _delivery(row)
            for row in conn.execute(
                select(directive_deliveries)
                .where(
                    directive_deliveries.c.task_id == task_id,
                    directive_deliveries.c.directive_id == directive.id,
                )
                .order_by(directive_deliveries.c.created_at)
            ).mappings()
        ]
        return directive
