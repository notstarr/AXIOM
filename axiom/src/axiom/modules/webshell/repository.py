from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.webshell.schemas import (
    WebShellConnection,
    WebShellExecLog,
    WebShellRequestTemplate,
    WebShellResponseTemplate,
)
from axiom.modules.webshell.tables import webshell_connections, webshell_exec_logs
from axiom.shared.ids import allocate_id


class WebShellRepository:
    def create(
        self,
        conn: Connection,
        *,
        task_id: str,
        name: str,
        url: str,
        family: str,
        request: WebShellRequestTemplate,
        response: WebShellResponseTemplate,
        remark: str,
        created_by: str,
        source_intent_id: str | None,
        now: str,
    ) -> WebShellConnection:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="webshell", prefix="ws"),
            "task_id": task_id,
            "name": name,
            "url": url,
            "family": family,
            "request_json": request.model_dump_json(),
            "response_json": response.model_dump_json(),
            "status": "untested",
            "last_check_at": None,
            "last_check_summary": "",
            "remark": remark,
            "created_by": created_by,
            "source_intent_id": source_intent_id,
            "created_at": now,
            "updated_at": now,
            "archived": False,
        }
        conn.execute(webshell_connections.insert().values(**values))
        return self._model(values)

    def get(self, conn: Connection, task_id: str, connection_id: str) -> WebShellConnection | None:
        row = conn.execute(
            select(webshell_connections).where(
                webshell_connections.c.task_id == task_id,
                webshell_connections.c.id == connection_id,
            )
        ).mappings().one_or_none()
        return self._model(dict(row)) if row is not None else None

    def list(self, conn: Connection, task_id: str, *, include_archived: bool = False) -> list[WebShellConnection]:
        query = select(webshell_connections).where(webshell_connections.c.task_id == task_id)
        if not include_archived:
            query = query.where(webshell_connections.c.archived.is_(False))
        rows = conn.execute(query.order_by(webshell_connections.c.id)).mappings()
        return [self._model(dict(row)) for row in rows]

    def update(self, conn: Connection, task_id: str, connection_id: str, values: dict, *, now: str) -> WebShellConnection | None:
        values = dict(values)
        values["updated_at"] = now
        result = conn.execute(
            update(webshell_connections)
            .where(
                webshell_connections.c.task_id == task_id,
                webshell_connections.c.id == connection_id,
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        return self.get(conn, task_id, connection_id)

    def add_log(
        self,
        conn: Connection,
        *,
        task_id: str,
        connection_id: str,
        command: str,
        output: str,
        ok: bool,
        duration_ms: int,
        actor: str,
        intent_id: str | None,
        now: str,
    ) -> WebShellExecLog:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="webshell_log", prefix="wl"),
            "task_id": task_id,
            "connection_id": connection_id,
            "command": command,
            "output": output[:128_000],
            "ok": ok,
            "duration_ms": duration_ms,
            "actor": actor,
            "intent_id": intent_id,
            "created_at": now,
        }
        conn.execute(webshell_exec_logs.insert().values(**values))
        return WebShellExecLog(
            id=values["id"],
            connection_id=connection_id,
            command=command,
            output=values["output"],
            ok=ok,
            duration_ms=duration_ms,
            actor=actor,
            intent_id=intent_id,
            created_at=now,
        )

    def logs(self, conn: Connection, task_id: str, connection_id: str, limit: int = 50) -> list[WebShellExecLog]:
        rows = conn.execute(
            select(webshell_exec_logs)
            .where(
                webshell_exec_logs.c.task_id == task_id,
                webshell_exec_logs.c.connection_id == connection_id,
            )
            .order_by(webshell_exec_logs.c.id.desc())
            .limit(limit)
        ).mappings()
        return [WebShellExecLog.model_validate(dict(row)) for row in rows]

    @staticmethod
    def _model(row: dict) -> WebShellConnection:
        return WebShellConnection(
            id=row["id"],
            task_id=row["task_id"],
            name=row["name"],
            url=row["url"],
            family=row.get("family") or "auto",
            request=WebShellRequestTemplate.model_validate(
                json.loads(row["request_json"]) if isinstance(row["request_json"], str) else row["request_json"]
            ),
            response=WebShellResponseTemplate.model_validate(
                json.loads(row["response_json"]) if isinstance(row["response_json"], str) else row["response_json"]
            ),
            status=row.get("status") or "untested",
            last_check_at=row.get("last_check_at"),
            last_check_summary=row.get("last_check_summary") or "",
            remark=row.get("remark") or "",
            created_by=row["created_by"],
            source_intent_id=row.get("source_intent_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived=bool(row.get("archived", False)),
        )
