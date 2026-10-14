from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.redteam.schemas import (
    RedTeamChain,
    RedTeamChainCreate,
    RedTeamResult,
    RedTeamResultReport,
)
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.shared.ids import allocate_id


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return _json_list(decoded)
    return []


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


class RedTeamRepository:
    def create_chain(
        self,
        conn: Connection,
        *,
        task_id: str,
        goal_cycle_id: str | None,
        command: RedTeamChainCreate,
        created_by: str,
        now: str,
    ) -> RedTeamChain:
        chain_id = allocate_id(conn, scope="global", kind="red_team_chain", prefix="ch", width=6)
        values = {
            "id": chain_id,
            "task_id": task_id,
            "goal_cycle_id": goal_cycle_id,
            "title": command.title,
            "initial_principal_json": command.initial_principal.model_dump(mode="json"),
            "target_capabilities_json": command.target_capabilities,
            "target_assets_json": command.target_assets,
            "status": "planned",
            "current_state_json": {
                "principal": command.initial_principal.model_dump(mode="json"),
                "capabilities": [],
                "assets": [],
            },
            "source_fact_ids_json": command.source_fact_ids,
            "source_intent_ids_json": [],
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(red_team_chains.insert().values(**values))
        return self._chain(values)

    def get_chain(self, conn: Connection, task_id: str, chain_id: str) -> RedTeamChain | None:
        row = conn.execute(
            select(red_team_chains).where(
                red_team_chains.c.task_id == task_id,
                red_team_chains.c.id == chain_id,
            )
        ).mappings().one_or_none()
        return self._chain(row) if row is not None else None

    def list_chains(self, conn: Connection, task_id: str) -> list[RedTeamChain]:
        rows = conn.execute(
            select(red_team_chains)
            .where(red_team_chains.c.task_id == task_id)
            .order_by(red_team_chains.c.created_at, red_team_chains.c.id)
        ).mappings()
        return [self._chain(row) for row in rows]

    def update_chain(
        self,
        conn: Connection,
        *,
        task_id: str,
        chain_id: str,
        values: dict,
    ) -> RedTeamChain | None:
        result = conn.execute(
            update(red_team_chains)
            .where(
                red_team_chains.c.task_id == task_id,
                red_team_chains.c.id == chain_id,
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        return self.get_chain(conn, task_id, chain_id)

    def get_result(self, conn: Connection, task_id: str, result_id: str) -> RedTeamResult | None:
        row = conn.execute(
            select(red_team_results).where(
                red_team_results.c.task_id == task_id,
                red_team_results.c.id == result_id,
            )
        ).mappings().one_or_none()
        return self._result(row) if row is not None else None

    def list_results(
        self,
        conn: Connection,
        task_id: str,
        *,
        chain_id: str | None = None,
    ) -> list[RedTeamResult]:
        query = select(red_team_results).where(red_team_results.c.task_id == task_id)
        if chain_id:
            query = query.where(red_team_results.c.chain_id == chain_id)
        rows = conn.execute(query.order_by(red_team_results.c.created_at, red_team_results.c.id)).mappings()
        return [self._result(row) for row in rows]

    def create_result(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        command: RedTeamResultReport,
        reported_by: str,
        now: str,
    ) -> RedTeamResult:
        result_id = allocate_id(conn, scope="global", kind="red_team_result", prefix="rr", width=6)
        values = {
            "id": result_id,
            "task_id": task_id,
            "chain_id": command.chain_id,
            "intent_id": intent_id,
            "result_type": command.result_type,
            "title": command.title,
            "description": command.description,
            "principal": command.principal,
            "tenant": command.tenant,
            "capabilities_before_json": command.capabilities_before,
            "capabilities_gained_json": command.capabilities_gained,
            "capabilities_after_json": command.capabilities_after,
            "target_asset": command.target_asset,
            "data_target_id": command.data_target_id,
            "sample_count": command.sample_count,
            "sample_bytes": command.sample_bytes,
            "data_fields_json": command.data_fields,
            "data_summary": command.data_summary,
            "credential_ref": command.credential_ref,
            "evidence_refs_json": command.evidence_refs,
            # The control plane immediately queues a Reviewer; there is no
            # externally observable window in which an unqueued Result is
            # considered complete.
            "status": "reviewing",
            "reported_by": reported_by,
            "reviewed_by": None,
            "review_reason": "",
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(red_team_results.insert().values(**values))
        return self._result(values)

    def set_result_status(
        self,
        conn: Connection,
        *,
        task_id: str,
        result_id: str,
        status: str,
        reviewed_by: str,
        review_reason: str,
        now: str,
    ) -> RedTeamResult | None:
        result = conn.execute(
            update(red_team_results)
            .where(
                red_team_results.c.task_id == task_id,
                red_team_results.c.id == result_id,
            )
            .values(
                status=status,
                reviewed_by=reviewed_by,
                review_reason=review_reason,
                updated_at=now,
            )
        )
        if not result.rowcount:
            return None
        return self.get_result(conn, task_id, result_id)

    @staticmethod
    def _chain(row) -> RedTeamChain:
        return RedTeamChain(
            id=row["id"],
            task_id=row["task_id"],
            goal_cycle_id=row.get("goal_cycle_id"),
            title=row["title"],
            initial_principal=_json_object(row.get("initial_principal_json")),
            target_capabilities=_json_list(row.get("target_capabilities_json")),
            target_assets=_json_list(row.get("target_assets_json")),
            status=row["status"],
            current_state=_json_object(row.get("current_state_json")),
            source_fact_ids=_json_list(row.get("source_fact_ids_json")),
            source_intent_ids=_json_list(row.get("source_intent_ids_json")),
            created_by=row["created_by"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _result(row) -> RedTeamResult:
        return RedTeamResult(
            id=row["id"],
            task_id=row["task_id"],
            chain_id=row["chain_id"],
            intent_id=row["intent_id"],
            result_type=row["result_type"],
            title=row["title"],
            description=row["description"],
            principal=row["principal"],
            tenant=row.get("tenant") or "",
            capabilities_before=_json_list(row.get("capabilities_before_json")),
            capabilities_gained=_json_list(row.get("capabilities_gained_json")),
            capabilities_after=_json_list(row.get("capabilities_after_json")),
            target_asset=row.get("target_asset") or "",
            data_target_id=row.get("data_target_id"),
            sample_count=row.get("sample_count"),
            sample_bytes=row.get("sample_bytes"),
            data_fields=_json_list(row.get("data_fields_json")),
            data_summary=row.get("data_summary") or "",
            credential_ref=row.get("credential_ref"),
            evidence_refs=_json_list(row.get("evidence_refs_json")),
            status=row["status"],
            reported_by=row["reported_by"],
            reviewed_by=row.get("reviewed_by"),
            review_reason=row.get("review_reason") or "",
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
