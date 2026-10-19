from __future__ import annotations

import json

from sqlalchemy import Connection, select, update

from axiom.modules.c2.schemas import C2Listener, C2Session, C2Task
from axiom.modules.c2.tables import c2_listeners, c2_sessions, c2_tasks
from axiom.shared.ids import allocate_id


class C2Repository:
    # ------------------------------------------------------------ listeners
    def create_listener(
        self,
        conn: Connection,
        *,
        task_id: str,
        name: str,
        kind: str,
        host: str,
        port: int,
        created_by: str,
        now: str,
    ) -> C2Listener:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="c2_listener", prefix="cl"),
            "task_id": task_id,
            "name": name,
            "kind": kind,
            "host": host,
            "port": port,
            "status": "stopped",
            "error": "",
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
            "archived": False,
        }
        conn.execute(c2_listeners.insert().values(**values))
        return C2Listener.model_validate(values)

    def get_listener(self, conn: Connection, task_id: str, listener_id: str) -> C2Listener | None:
        row = conn.execute(
            select(c2_listeners).where(
                c2_listeners.c.task_id == task_id,
                c2_listeners.c.id == listener_id,
            )
        ).mappings().one_or_none()
        return C2Listener.model_validate(dict(row)) if row is not None else None

    def list_listeners(self, conn: Connection, task_id: str, *, include_archived: bool = False) -> list[C2Listener]:
        query = select(c2_listeners).where(c2_listeners.c.task_id == task_id)
        if not include_archived:
            query = query.where(c2_listeners.c.archived.is_(False))
        rows = conn.execute(query.order_by(c2_listeners.c.id)).mappings()
        return [C2Listener.model_validate(dict(row)) for row in rows]

    def update_listener(self, conn: Connection, task_id: str, listener_id: str, values: dict, *, now: str) -> C2Listener | None:
        values = dict(values)
        values["updated_at"] = now
        result = conn.execute(
            update(c2_listeners)
            .where(c2_listeners.c.task_id == task_id, c2_listeners.c.id == listener_id)
            .values(**values)
        )
        if not result.rowcount:
            return None
        return self.get_listener(conn, task_id, listener_id)

    # ------------------------------------------------------------ sessions
    def create_session(
        self,
        conn: Connection,
        *,
        task_id: str,
        listener_id: str,
        kind: str,
        remote: str,
        info: dict | None = None,
        now: str,
    ) -> C2Session:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="c2_session", prefix="cs"),
            "task_id": task_id,
            "listener_id": listener_id,
            "kind": kind,
            "remote": remote,
            "info_json": json.dumps(info or {}, ensure_ascii=False),
            "status": "active",
            "sleep_seconds": 5,
            "last_seen_at": now,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(c2_sessions.insert().values(**values))
        return self._session(dict(values))

    def get_session(self, conn: Connection, task_id: str, session_id: str) -> C2Session | None:
        row = conn.execute(
            select(c2_sessions).where(
                c2_sessions.c.task_id == task_id,
                c2_sessions.c.id == session_id,
            )
        ).mappings().one_or_none()
        return self._session(dict(row)) if row is not None else None

    def session_by_remote(self, conn: Connection, task_id: str, listener_id: str, remote: str) -> C2Session | None:
        row = conn.execute(
            select(c2_sessions).where(
                c2_sessions.c.task_id == task_id,
                c2_sessions.c.listener_id == listener_id,
                c2_sessions.c.remote == remote,
            )
        ).mappings().one_or_none()
        return self._session(dict(row)) if row is not None else None

    def list_sessions(self, conn: Connection, task_id: str, *, listener_id: str | None = None) -> list[C2Session]:
        query = select(c2_sessions).where(c2_sessions.c.task_id == task_id)
        if listener_id:
            query = query.where(c2_sessions.c.listener_id == listener_id)
        rows = conn.execute(query.order_by(c2_sessions.c.id)).mappings()
        return [self._session(dict(row)) for row in rows]

    def update_session(self, conn: Connection, task_id: str, session_id: str, values: dict, *, now: str) -> C2Session | None:
        values = dict(values)
        values["updated_at"] = now
        result = conn.execute(
            update(c2_sessions)
            .where(c2_sessions.c.task_id == task_id, c2_sessions.c.id == session_id)
            .values(**values)
        )
        if not result.rowcount:
            return None
        return self.get_session(conn, task_id, session_id)

    # ------------------------------------------------------------ tasks
    def create_task(
        self,
        conn: Connection,
        *,
        task_id: str,
        session_id: str,
        command: str,
        actor: str,
        intent_id: str | None,
        now: str,
    ) -> C2Task:
        values = {
            "id": allocate_id(conn, scope=task_id, kind="c2_task", prefix="ct"),
            "task_id": task_id,
            "session_id": session_id,
            "command": command,
            "status": "queued",
            "output": "",
            "duration_ms": 0,
            "actor": actor,
            "intent_id": intent_id,
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(c2_tasks.insert().values(**values))
        return C2Task.model_validate(values)

    def get_task(self, conn: Connection, task_id: str, c2_task_id: str) -> C2Task | None:
        row = conn.execute(
            select(c2_tasks).where(
                c2_tasks.c.task_id == task_id,
                c2_tasks.c.id == c2_task_id,
            )
        ).mappings().one_or_none()
        return C2Task.model_validate(dict(row)) if row is not None else None

    def list_tasks(self, conn: Connection, task_id: str, *, session_id: str | None = None, limit: int = 50) -> list[C2Task]:
        query = select(c2_tasks).where(c2_tasks.c.task_id == task_id)
        if session_id:
            query = query.where(c2_tasks.c.session_id == session_id)
        rows = conn.execute(query.order_by(c2_tasks.c.id.desc()).limit(limit)).mappings()
        return [C2Task.model_validate(dict(row)) for row in rows]

    def pop_queued_task(self, conn: Connection, task_id: str, session_id: str, *, now: str) -> C2Task | None:
        row = conn.execute(
            select(c2_tasks)
            .where(
                c2_tasks.c.task_id == task_id,
                c2_tasks.c.session_id == session_id,
                c2_tasks.c.status == "queued",
            )
            .order_by(c2_tasks.c.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).mappings().one_or_none()
        if row is None:
            return None
        conn.execute(
            update(c2_tasks)
            .where(c2_tasks.c.task_id == task_id, c2_tasks.c.id == row["id"])
            .values(status="sent", updated_at=now)
        )
        return C2Task.model_validate({**dict(row), "status": "sent", "updated_at": now})

    def finish_task(
        self,
        conn: Connection,
        task_id: str,
        c2_task_id: str,
        *,
        ok: bool,
        output: str,
        duration_ms: int,
        now: str,
    ) -> C2Task | None:
        result = conn.execute(
            update(c2_tasks)
            .where(c2_tasks.c.task_id == task_id, c2_tasks.c.id == c2_task_id)
            .values(
                status="completed" if ok else "failed",
                output=output[:200_000],
                duration_ms=duration_ms,
                updated_at=now,
            )
        )
        if not result.rowcount:
            return None
        return self.get_task(conn, task_id, c2_task_id)

    @staticmethod
    def _session(row: dict) -> C2Session:
        info = row.get("info_json")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except (TypeError, ValueError):
                info = {}
        return C2Session(
            id=row["id"],
            task_id=row["task_id"],
            listener_id=row["listener_id"],
            kind=row.get("kind") or "http",
            remote=row.get("remote") or "",
            info=info or {},
            status=row.get("status") or "active",
            sleep_seconds=int(row.get("sleep_seconds") or 5),
            last_seen_at=row.get("last_seen_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
