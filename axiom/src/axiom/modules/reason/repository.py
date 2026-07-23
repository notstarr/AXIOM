from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.graph.tables import intent_sources, intents
from axiom.modules.reason.schemas import ReasonWorkspace, WorkspaceDraft
from axiom.modules.reason.tables import reason_workspaces
from axiom.shared.ids import allocate_id


class ReasonRepository:
    def create_intent(
        self,
        conn: Connection,
        *,
        task_id: str,
        source_fact_ids: list[str],
        worker_pool_json: str,
        target: str,
        description: str,
        creator: str,
        now: str,
    ) -> str:
        intent_id = allocate_id(conn, scope=task_id, kind="intent", prefix="i")
        conn.execute(
            intents.insert().values(
                id=intent_id,
                task_id=task_id,
                todo_id=None,
                kind="reason",
                description=description,
                status="pending",
                creator=creator,
                worker=None,
                worker_pool_json=worker_pool_json,
                targets_json=json.dumps([target], ensure_ascii=False),
                retry_count=0,
                infrastructure_retry_count=0,
                last_failure_kind="",
                failure_class="",
                created_at=now,
                archived=False,
            )
        )
        sources = list(dict.fromkeys([*source_fact_ids, "goal"]))
        conn.execute(
            intent_sources.insert(),
            [
                {"task_id": task_id, "intent_id": intent_id, "fact_id": fact_id}
                for fact_id in sources
            ],
        )
        return intent_id

    def open(
        self,
        conn: Connection,
        *,
        task_id: str,
        intent_id: str,
        lease_id: str,
        worker: str,
        context_revision: int,
        now: str,
    ) -> ReasonWorkspace:
        workspace_id = allocate_id(
            conn,
            scope="global",
            kind="reason_workspace",
            prefix="rw_",
            width=6,
        )
        values = {
            "id": workspace_id,
            "task_id": task_id,
            "intent_id": intent_id,
            "lease_id": lease_id,
            "status": "open",
            "context_revision": context_revision,
            "draft_json": WorkspaceDraft().model_dump_json(),
            "worker": worker,
            "failure_reason": "",
            "result_json": "{}",
            "created_at": now,
            "updated_at": now,
            "committed_at": None,
        }
        conn.execute(reason_workspaces.insert().values(**values))
        return self._workspace(values)

    def get(self, conn: Connection, workspace_id: str) -> ReasonWorkspace | None:
        row = conn.execute(
            select(reason_workspaces).where(reason_workspaces.c.id == workspace_id)
        ).mappings().one_or_none()
        return self._workspace(row) if row is not None else None

    def for_attempt(
        self,
        conn: Connection,
        task_id: str,
        intent_id: str,
        lease_id: str,
    ) -> ReasonWorkspace | None:
        row = conn.execute(
            select(reason_workspaces).where(
                reason_workspaces.c.task_id == task_id,
                reason_workspaces.c.intent_id == intent_id,
                reason_workspaces.c.lease_id == lease_id,
            )
        ).mappings().one_or_none()
        return self._workspace(row) if row is not None else None

    def save_draft(
        self,
        conn: Connection,
        workspace_id: str,
        draft: WorkspaceDraft,
        *,
        now: str,
    ) -> ReasonWorkspace:
        result = conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id, reason_workspaces.c.status == "open")
            .values(draft_json=draft.model_dump_json(), updated_at=now)
        )
        if not result.rowcount:
            raise ValueError("Reason Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    def mark_committed(
        self,
        conn: Connection,
        workspace_id: str,
        *,
        created_todo_ids: list[str],
        created_intent_ids: list[str],
        created_vulnerability_ids: list[str],
        now: str,
    ) -> ReasonWorkspace:
        result_json = json.dumps(
            {
                "created_todo_ids": created_todo_ids,
                "created_intent_ids": created_intent_ids,
                "created_vulnerability_ids": created_vulnerability_ids,
            },
            ensure_ascii=False,
        )
        result = conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id, reason_workspaces.c.status == "open")
            .values(
                status="committed",
                result_json=result_json,
                updated_at=now,
                committed_at=now,
            )
        )
        if not result.rowcount:
            raise ValueError("Reason Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    def commit_result(
        self,
        conn: Connection,
        workspace: ReasonWorkspace,
    ) -> tuple[list[str], list[str], list[str]]:
        row = conn.execute(
            select(reason_workspaces.c.result_json).where(reason_workspaces.c.id == workspace.id)
        ).scalar_one()
        payload = json.loads(row or "{}")
        return (
            list(payload.get("created_todo_ids", [])),
            list(payload.get("created_intent_ids", [])),
            list(payload.get("created_vulnerability_ids", [])),
        )

    def abandon(
        self,
        conn: Connection,
        workspace_id: str,
        *,
        failure_reason: str,
        now: str,
    ) -> ReasonWorkspace:
        result = conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id, reason_workspaces.c.status == "open")
            .values(status="abandoned", failure_reason=failure_reason, updated_at=now)
        )
        if not result.rowcount:
            raise ValueError("Reason Workspace is not open")
        workspace = self.get(conn, workspace_id)
        assert workspace is not None
        return workspace

    @staticmethod
    def _workspace(row) -> ReasonWorkspace:
        return ReasonWorkspace(
            id=row["id"],
            task_id=row["task_id"],
            intent_id=row["intent_id"],
            lease_id=row["lease_id"],
            status=row["status"],
            context_revision=row["context_revision"],
            draft=WorkspaceDraft.model_validate_json(row["draft_json"] or "{}"),
            worker=row["worker"],
            failure_reason=row["failure_reason"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            committed_at=row["committed_at"],
        )
