from __future__ import annotations

import json

from sqlalchemy import Connection, and_, insert, or_, select, update
from sqlalchemy.engine import RowMapping

from axiom.modules.directives.schemas import (
    DeliverySummary,
    Directive,
    DirectiveItem,
    EligibleExplore,
    PERSISTENT_SCOPES,
    WorkerDirectiveBundle,
)
from axiom.modules.directives.tables import directive_deliveries, worker_directives
from axiom.modules.graph.tables import intents
from axiom.shared.ids import allocate_id


MAX_ONCE_PER_DELIVERY = 20
_ACTIVE_DELIVERY_STATUSES = ("queued", "delivered")


def _directive(row: RowMapping | dict) -> Directive:
    return Directive(
        id=row["id"],
        task_id=row["task_id"],
        content=row["content"],
        creator=row["creator"],
        scope=row["scope"],
        lifetime=row["lifetime"],
        target_intent_id=row["target_intent_id"],
        status=row["status"],
        client_message_id=row["client_message_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        delivered_at=row["delivered_at"],
        delivered_run_id=row["delivered_run_id"],
        disabled_at=row["disabled_at"],
        expires_at=row["expires_at"],
        archived=bool(row["archived"]),
    )


def _delivery(row: RowMapping | dict) -> DeliverySummary:
    return DeliverySummary(
        id=row["id"],
        delivery_kind=row["delivery_kind"],
        reason_run_id=row["reason_run_id"],
        intent_id=row["intent_id"],
        lease_id=row["lease_id"],
        status=row["status"],
        created_at=row["created_at"],
    )


class DirectiveRepository:
    # ── inventory ───────────────────────────────────────────────────────────

    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
        content: str,
        creator: str,
        scope: str,
        lifetime: str,
        target_intent_id: str | None,
        status: str,
        client_message_id: str | None,
        expires_at: str | None,
        now: str,
    ) -> Directive:
        directive_id = allocate_id(
            conn, scope="global", kind="directive", prefix="dir_", width=6
        )
        values = {
            "id": directive_id,
            "task_id": task_id,
            "content": content,
            "creator": creator,
            "scope": scope,
            "lifetime": lifetime,
            "target_intent_id": target_intent_id,
            "status": status,
            "client_message_id": client_message_id,
            "created_at": now,
            "updated_at": None,
            "delivered_at": None,
            "delivered_run_id": None,
            "disabled_at": None,
            "expires_at": expires_at,
            "archived": False,
        }
        conn.execute(insert(worker_directives).values(**values))
        return _directive(values)

    def by_client_id(
        self, conn: Connection, task_id: str, client_message_id: str
    ) -> Directive | None:
        row = conn.execute(
            select(worker_directives).where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.client_message_id == client_message_id,
            )
        ).mappings().one_or_none()
        return _directive(row) if row is not None else None

    def get(self, conn: Connection, task_id: str, directive_id: str) -> Directive | None:
        row = conn.execute(
            select(worker_directives).where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.id == directive_id,
            )
        ).mappings().one_or_none()
        return _directive(row) if row is not None else None

    def list(
        self, conn: Connection, task_id: str, *, status: str | None = None
    ) -> list[Directive]:
        # 归档（delete）是软删：列表只返回未归档的，已归档的仅保留审计可查。
        query = select(worker_directives).where(
            worker_directives.c.task_id == task_id,
            worker_directives.c.archived.is_(False),
        )
        if status:
            query = query.where(worker_directives.c.status == status)
        rows = conn.execute(
            query.order_by(worker_directives.c.created_at, worker_directives.c.id)
        ).mappings().all()
        result = []
        for row in rows:
            item = _directive(row)
            item.deliveries = [
                _delivery(delivery)
                for delivery in conn.execute(
                    select(directive_deliveries)
                    .where(directive_deliveries.c.directive_id == item.id)
                    .order_by(directive_deliveries.c.created_at)
                ).mappings()
            ]
            result.append(item)
        return result

    def update(
        self,
        conn: Connection,
        task_id: str,
        directive_id: str,
        values: dict,
        *,
        now: str,
    ) -> bool:
        payload = {**values, "updated_at": now}
        result = conn.execute(
            update(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.id == directive_id,
            )
            .values(**payload)
        )
        return bool(result.rowcount)

    def intent_status(
        self, conn: Connection, task_id: str, intent_id: str
    ) -> dict | None:
        row = conn.execute(
            select(intents.c.id, intents.c.kind, intents.c.status, intents.c.archived)
            .where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def eligible_explores(self, conn: Connection, task_id: str) -> list[EligibleExplore]:
        rows = conn.execute(
            select(
                intents.c.id,
                intents.c.description,
                intents.c.targets_json,
                intents.c.created_at,
            )
            .where(
                intents.c.task_id == task_id,
                intents.c.kind == "explore",
                intents.c.status == "pending",
                intents.c.archived.is_(False),
            )
            .order_by(intents.c.created_at, intents.c.id)
        ).mappings().all()
        output = []
        for row in rows:
            try:
                targets = json.loads(row["targets_json"] or "[]")
            except (TypeError, ValueError):
                targets = []
            output.append(
                EligibleExplore(
                    intent_id=row["id"],
                    description=row["description"] or "",
                    targets=targets if isinstance(targets, list) else [],
                    created_at=row["created_at"] or "",
                )
            )
        return output

    # ── one-shot delivery ───────────────────────────────────────────────────

    def _insert_delivery(
        self,
        conn: Connection,
        *,
        task_id: str,
        directive_id: str,
        delivery_kind: str,
        reason_run_id: str | None,
        intent_id: str | None,
        lease_id: str | None,
        now: str,
    ) -> None:
        delivery_id = allocate_id(
            conn, scope="global", kind="directive_delivery", prefix="dd_", width=6
        )
        conn.execute(
            insert(directive_deliveries).values(
                id=delivery_id,
                task_id=task_id,
                directive_id=directive_id,
                delivery_kind=delivery_kind,
                reason_run_id=reason_run_id,
                intent_id=intent_id,
                lease_id=lease_id,
                status="queued",
                created_at=now,
            )
        )

    def consume_for_reason_run(
        self,
        conn: Connection,
        *,
        task_id: str,
        reason_run_id: str,
        now: str,
    ) -> list[dict]:
        """Atomically attach all queued ``reason_next`` directives to a NEW run.

        Called only from ``ReasonService._create_run``: the coalesce and queue-tail
        branches never create a run, so instructions survive them untouched.
        """
        rows = conn.execute(
            select(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.scope == "reason_next",
                worker_directives.c.status == "pending",
                worker_directives.c.archived.is_(False),
                or_(
                    worker_directives.c.expires_at.is_(None),
                    worker_directives.c.expires_at > now,
                ),
            )
            .order_by(worker_directives.c.created_at, worker_directives.c.id)
            .limit(MAX_ONCE_PER_DELIVERY)
            .with_for_update()
        ).mappings().all()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        conn.execute(
            update(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.id.in_(ids),
            )
            .values(
                status="delivered",
                delivered_at=now,
                delivered_run_id=reason_run_id,
                updated_at=now,
            )
        )
        for row in rows:
            self._insert_delivery(
                conn,
                task_id=task_id,
                directive_id=row["id"],
                delivery_kind="reason_next",
                reason_run_id=reason_run_id,
                intent_id=None,
                lease_id=None,
                now=now,
            )
        return [dict(row) for row in rows]

    def consume_for_intent(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        now: str,
    ) -> list[dict]:
        """Atomically attach ``explore_one`` directives at claim time."""
        rows = conn.execute(
            select(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.scope == "explore_one",
                worker_directives.c.target_intent_id == intent_id,
                worker_directives.c.status == "pending",
                worker_directives.c.archived.is_(False),
                or_(
                    worker_directives.c.expires_at.is_(None),
                    worker_directives.c.expires_at > now,
                ),
            )
            .order_by(worker_directives.c.created_at, worker_directives.c.id)
            .limit(MAX_ONCE_PER_DELIVERY)
            .with_for_update()
        ).mappings().all()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        conn.execute(
            update(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.id.in_(ids),
            )
            .values(status="delivered", delivered_at=now, updated_at=now)
        )
        for row in rows:
            self._insert_delivery(
                conn,
                task_id=task_id,
                directive_id=row["id"],
                delivery_kind="explore_one",
                reason_run_id=None,
                intent_id=intent_id,
                lease_id=lease_id,
                now=now,
            )
        return [dict(row) for row in rows]

    def resolve(
        self,
        conn: Connection,
        *,
        task_id: str,
        phase: str,
        lease_id: str,
        intent_id: str | None = None,
        reason_run_id: str | None = None,
        now: str,
    ) -> WorkerDirectiveBundle:
        """Pure read. Persistent rows are re-read; one-shot rows come from the ledger."""
        bundle = WorkerDirectiveBundle()
        persistent = conn.execute(
            select(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.lifetime == "persistent",
                worker_directives.c.status == "active",
                worker_directives.c.archived.is_(False),
                worker_directives.c.scope.in_(sorted(PERSISTENT_SCOPES)),
                or_(
                    worker_directives.c.expires_at.is_(None),
                    worker_directives.c.expires_at > now,
                ),
            )
            .order_by(worker_directives.c.created_at, worker_directives.c.id)
        ).mappings().all()
        for row in persistent:
            item = DirectiveItem(id=row["id"], content=row["content"])
            if row["scope"] == "all_workers":
                bundle.all_workers.append(item)
            elif row["scope"] == "reason_all" and phase == "reason":
                bundle.reason_all.append(item)
            elif row["scope"] == "explore_all" and phase == "explore":
                bundle.explore_all.append(item)

        conditions = []
        if reason_run_id:
            conditions.append(
                and_(
                    directive_deliveries.c.delivery_kind == "reason_next",
                    directive_deliveries.c.reason_run_id == reason_run_id,
                )
            )
        if intent_id and lease_id:
            conditions.append(
                and_(
                    directive_deliveries.c.delivery_kind == "explore_one",
                    directive_deliveries.c.intent_id == intent_id,
                    directive_deliveries.c.lease_id == lease_id,
                )
            )
        if not conditions:
            return bundle

        join_condition = and_(
            directive_deliveries.c.task_id == worker_directives.c.task_id,
            directive_deliveries.c.directive_id == worker_directives.c.id,
        )
        rows = conn.execute(
            select(
                directive_deliveries.c.delivery_kind,
                worker_directives.c.id,
                worker_directives.c.content,
            )
            .select_from(directive_deliveries.join(worker_directives, join_condition))
            .where(
                directive_deliveries.c.task_id == task_id,
                directive_deliveries.c.status.in_(_ACTIVE_DELIVERY_STATUSES),
                or_(*conditions),
            )
            .order_by(directive_deliveries.c.created_at, directive_deliveries.c.id)
        ).mappings().all()
        for row in rows:
            item = DirectiveItem(id=row["id"], content=row["content"])
            if row["delivery_kind"] == "reason_next" and phase == "reason":
                bundle.reason_once.append(item)
            elif row["delivery_kind"] == "explore_one" and phase == "explore":
                bundle.explore_once.append(item)
        return bundle

    def mark_delivered(
        self,
        conn: Connection,
        *,
        task_id: str,
        lease_id: str | None = None,
        reason_run_id: str | None = None,
        now: str,
    ) -> None:
        conditions = []
        if lease_id:
            conditions.append(directive_deliveries.c.lease_id == lease_id)
        if reason_run_id:
            conditions.append(directive_deliveries.c.reason_run_id == reason_run_id)
        if not conditions:
            return
        conn.execute(
            update(directive_deliveries)
            .where(
                directive_deliveries.c.task_id == task_id,
                directive_deliveries.c.status == "queued",
                or_(*conditions),
            )
            .values(status="delivered", delivered_at=now)
        )

    def reopen_queued(
        self,
        conn: Connection,
        *,
        task_id: str,
        lease_id: str | None = None,
        reason_run_id: str | None = None,
        now: str,
    ) -> list[str]:
        """Re-arm one-shot directives whose lease died before the worker started.

        The old delivery rows are kept (marked ``reopened``) so the ledger still
        explains why the instruction was delivered twice.
        """
        conditions = []
        if lease_id:
            conditions.append(directive_deliveries.c.lease_id == lease_id)
        if reason_run_id:
            conditions.append(directive_deliveries.c.reason_run_id == reason_run_id)
        if not conditions:
            return []
        rows = conn.execute(
            select(directive_deliveries)
            .where(
                directive_deliveries.c.task_id == task_id,
                directive_deliveries.c.status == "queued",
                or_(*conditions),
            )
            .with_for_update()
        ).mappings().all()
        if not rows:
            return []
        ids = [row["directive_id"] for row in rows]
        delivery_ids = [row["id"] for row in rows]
        conn.execute(
            update(directive_deliveries)
            .where(directive_deliveries.c.id.in_(delivery_ids))
            .values(status="reopened", reopened_at=now)
        )
        conn.execute(
            update(worker_directives)
            .where(
                worker_directives.c.task_id == task_id,
                worker_directives.c.id.in_(ids),
                worker_directives.c.status == "delivered",
            )
            .values(
                status="pending",
                delivered_at=None,
                delivered_run_id=None,
                updated_at=now,
            )
        )
        return ids
