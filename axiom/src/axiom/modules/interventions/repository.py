from __future__ import annotations

from sqlalchemy import Connection, select, update

from axiom.modules.graph.tables import intents
from axiom.modules.interventions.schemas import CreateIntervention, Intervention
from axiom.modules.interventions.tables import intent_interventions
from axiom.shared.clock import utcnow


class InterventionRepository:
    @staticmethod
    def intent(conn: Connection, task_id: str, intent_id: str):
        return conn.execute(
            select(intents).where(intents.c.task_id == task_id, intents.c.id == intent_id)
        ).mappings().one_or_none()

    @staticmethod
    def get_by_client_id(conn: Connection, task_id: str, intent_id: str, client_message_id: str):
        row = conn.execute(
            select(intent_interventions).where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.intent_id == intent_id,
                intent_interventions.c.client_message_id == client_message_id,
            )
        ).mappings().one_or_none()
        return InterventionRepository.model(row) if row is not None else None

    @staticmethod
    def insert(conn: Connection, *, task_id: str, intent_id: str, lease_id: str, body: CreateIntervention, intervention_id: str, now: str) -> Intervention:
        values = {
            "id": intervention_id,
            "task_id": task_id,
            "intent_id": intent_id,
            "lease_id": lease_id,
            "actor": body.actor,
            "message": body.message,
            "client_message_id": body.client_message_id,
            "status": "pending",
            "error": "",
            "created_at": now,
            "sent_at": None,
            "updated_at": now,
        }
        conn.execute(intent_interventions.insert().values(**values))
        return Intervention.model_validate(values)

    @staticmethod
    def pending(conn: Connection, *, task_id: str, intent_id: str, lease_id: str, limit: int) -> list[Intervention]:
        rows = conn.execute(
            select(intent_interventions)
            .where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.intent_id == intent_id,
                intent_interventions.c.lease_id == lease_id,
                intent_interventions.c.status == "pending",
            )
            .order_by(intent_interventions.c.created_at, intent_interventions.c.id)
            .limit(max(1, min(limit, 20)))
        ).mappings().all()
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        now = utcnow()
        conn.execute(
            update(intent_interventions)
            .where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.id.in_(ids),
                intent_interventions.c.status == "pending",
            )
            .values(status="sent", sent_at=now, updated_at=now)
        )
        return [Intervention.model_validate({**dict(row), "status": "sent", "sent_at": now, "updated_at": now}) for row in rows]

    @staticmethod
    def resolve(conn: Connection, *, task_id: str, intervention_id: str, success: bool, error: str = "") -> Intervention | None:
        now = utcnow()
        result = conn.execute(
            update(intent_interventions)
            .where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.id == intervention_id,
                intent_interventions.c.status == "sent",
            )
            .values(status="accepted" if success else "failed", error=error[:1_000], updated_at=now)
        )
        if not result.rowcount:
            return None
        row = conn.execute(
            select(intent_interventions).where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.id == intervention_id,
            )
        ).mappings().one()
        return InterventionRepository.model(row)

    @staticmethod
    def expire_lease(conn: Connection, *, task_id: str, intent_id: str, lease_id: str) -> int:
        result = conn.execute(
            update(intent_interventions)
            .where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.intent_id == intent_id,
                intent_interventions.c.lease_id == lease_id,
                intent_interventions.c.status.in_(["pending", "sent"]),
            )
            .values(status="expired", error="Explore Lease 已结束", updated_at=utcnow())
        )
        return int(result.rowcount or 0)

    @staticmethod
    def list_for_intent(conn: Connection, *, task_id: str, intent_id: str, limit: int = 200) -> list[Intervention]:
        rows = conn.execute(
            select(intent_interventions)
            .where(
                intent_interventions.c.task_id == task_id,
                intent_interventions.c.intent_id == intent_id,
            )
            .order_by(intent_interventions.c.created_at, intent_interventions.c.id)
            .limit(max(1, min(limit, 500)))
        ).mappings().all()
        return [InterventionRepository.model(row) for row in rows]

    @staticmethod
    def model(row) -> Intervention:
        return Intervention.model_validate(dict(row))
