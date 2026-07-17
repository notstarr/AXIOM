from __future__ import annotations

from sqlalchemy import Connection, select, update

from axiom.infrastructure.database.metadata import reason_workspaces
from axiom.modules.common.ids import allocate_readable_id
from axiom.modules.reason_workspace.schemas import ReasonWorkspace, WorkspaceDraft


class ReasonWorkspaceRepository:
    def open(
        self,
        conn: Connection,
        *,
        task_id: str,
        worker: str,
        context_revision: int,
        now: str,
    ) -> ReasonWorkspace:
        workspace_id = allocate_readable_id(
            conn,
            scope="global",
            kind="reason_workspace",
            prefix="rw_",
            width=6,
        )
        values = {
            "id": workspace_id,
            "task_id": task_id,
            "status": "open",
            "context_revision": context_revision,
            "draft_json": WorkspaceDraft().model_dump_json(),
            "worker": worker,
            "failure_reason": "",
            "created_at": now,
            "updated_at": now,
            "committed_at": None,
        }
        conn.execute(reason_workspaces.insert().values(**values))
        return self._to_model(values)

    def get(self, conn: Connection, workspace_id: str) -> ReasonWorkspace | None:
        row = conn.execute(
            select(reason_workspaces).where(reason_workspaces.c.id == workspace_id)
        ).mappings().one_or_none()
        return self._to_model(row) if row is not None else None

    def save_draft(
        self,
        conn: Connection,
        workspace_id: str,
        draft: WorkspaceDraft,
        *,
        now: str,
    ) -> ReasonWorkspace:
        conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id)
            .values(draft_json=draft.model_dump_json(), updated_at=now)
        )
        return self.get(conn, workspace_id)  # type: ignore[return-value]

    def mark_committed(self, conn: Connection, workspace_id: str, *, now: str) -> ReasonWorkspace:
        conn.execute(
            update(reason_workspaces)
            .where(reason_workspaces.c.id == workspace_id)
            .values(status="committed", updated_at=now, committed_at=now)
        )
        return self.get(conn, workspace_id)  # type: ignore[return-value]

    @staticmethod
    def _to_model(row) -> ReasonWorkspace:
        return ReasonWorkspace(
            id=row["id"],
            task_id=row["task_id"],
            status=row["status"],
            context_revision=row["context_revision"],
            draft=WorkspaceDraft.model_validate_json(row["draft_json"]),
            worker=row["worker"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            committed_at=row["committed_at"],
        )
