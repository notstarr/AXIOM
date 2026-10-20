"""C2 listener threads: http polling beacon + tcp raw socket + watchdog."""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sqlalchemy import Engine, select

from axiom.modules.c2.repository import C2Repository
from axiom.modules.c2.tables import c2_sessions, c2_tasks
from axiom.modules.c2.token import c2_listener_token
from axiom.shared.clock import utcnow


logger = logging.getLogger(__name__)

_TCP_IDLE_SECONDS = 1.5


def _decode_b64(text: str) -> str:
    cleaned = "".join(text.split())
    if not cleaned:
        return ""
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return text


class ListenerManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = C2Repository()
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._session_owner: dict[str, str] = {}
        self._watchdog: threading.Thread | None = None

    # ---------------------------------------------------------- lifecycle
    def start_watchdog(self) -> None:
        if self._watchdog is not None:
            return
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True, name="c2-watchdog")
        self._watchdog.start()

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._entries)
        for listener_id in ids:
            try:
                self.stop(listener_id, mark_stopped=True)
            except Exception:  # noqa: BLE001
                logger.exception("stop listener %s failed", listener_id)

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(30)
            try:
                self._sweep()
            except Exception:  # noqa: BLE001
                logger.exception("c2 watchdog sweep failed")

    def _sweep(self) -> None:
        now = utcnow()
        with self._lock:
            live_listeners = set(self._entries)
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(c2_sessions).where(c2_sessions.c.status == "active")
            ).mappings().all()
            for row in rows:
                dead = False
                if row["listener_id"] not in live_listeners:
                    dead = True
                elif row["kind"] == "http" and row["last_seen_at"]:
                    try:
                        last = time.mktime(time.strptime(str(row["last_seen_at"])[:19], "%Y-%m-%dT%H:%M:%S"))
                        threshold = max(3 * int(row["sleep_seconds"] or 5), 120)
                        dead = time.time() - last > threshold + 30
                    except (ValueError, OverflowError):
                        dead = False
                if dead:
                    conn.execute(
                        c2_sessions.update()
                        .where(c2_sessions.c.task_id == row["task_id"], c2_sessions.c.id == row["id"])
                        .values(status="dead", updated_at=now)
                    )

    # ---------------------------------------------------------- start/stop
    def start(self, listener) -> None:
        with self._lock:
            if listener.id in self._entries:
                return
        if listener.kind == "http":
            manager = self
            task_id, listener_id = listener.task_id, listener.id
            expected_token = c2_listener_token(listener_id)

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *args) -> None:  # noqa: D102
                    pass

                def _sid(self) -> str:
                    query = parse_qs(urlparse(self.path).query)
                    return (query.get("sid") or [""])[0].strip()

                def _token(self) -> str:
                    query = parse_qs(urlparse(self.path).query)
                    return (query.get("token") or [""])[0].strip()

                def _authorized(self) -> bool:
                    supplied = self._token()
                    return bool(supplied) and hmac.compare_digest(supplied, expected_token)

                def _reject(self, status: int) -> None:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    self.close_connection = True

                def do_GET(self) -> None:  # noqa: N802
                    sid = self._sid()
                    if not sid:
                        self._reject(404)
                        return
                    if not self._authorized():
                        self._reject(403)
                        return
                    command = manager.http_poll(task_id, listener_id, sid)
                    payload = command.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_POST(self) -> None:  # noqa: N802
                    sid = self._sid()
                    if not sid:
                        self._reject(404)
                        return
                    if not self._authorized():
                        self._reject(403)
                        return
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                    manager.http_result(task_id, listener_id, sid, _decode_b64(raw))
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            try:
                server = ThreadingHTTPServer((listener.host, listener.port), Handler)
            except OSError as exc:
                self._mark_listener(listener, "error", str(exc))
                raise
            thread = threading.Thread(target=server.serve_forever, daemon=True, name=f"c2-http-{listener.id}")
            entry = {"kind": "http", "server": server, "thread": thread}
            thread.start()
        else:
            # Raw TCP C2 is a shell session without a handshake; token auth is
            # HTTP-only.  Do not expose TCP listeners on untrusted networks.
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((listener.host, listener.port))
                sock.listen(8)
            except OSError as exc:
                self._mark_listener(listener, "error", str(exc))
                raise
            thread = threading.Thread(
                target=self._tcp_accept_loop,
                args=(listener, sock),
                daemon=True,
                name=f"c2-tcp-{listener.id}",
            )
            entry = {"kind": "tcp", "socket": sock, "thread": thread}
            thread.start()
        with self._lock:
            self._entries[listener.id] = entry
        self._mark_listener(listener, "listening", "")

    def stop(self, listener_id: str, *, mark_stopped: bool = True) -> None:
        with self._lock:
            entry = self._entries.pop(listener_id, None)
            owned = [sid for sid, owner in self._session_owner.items() if owner == listener_id]
            for sid in owned:
                self._session_owner.pop(sid, None)
        if entry is None:
            return
        session_entries = entry.get("sessions", {})
        if entry["kind"] == "http":
            entry["server"].shutdown()
            entry["server"].server_close()
        else:
            for item in session_entries.values():
                try:
                    item["conn"].close()
                except OSError:
                    pass
            try:
                entry["socket"].close()
            except OSError:
                pass
        if mark_stopped:
            self._mark_listener_by_id(listener_id, "stopped", "")
            self._kill_sessions(listener_id, owned_session_ids=None)

    def is_running(self, listener_id: str) -> bool:
        with self._lock:
            return listener_id in self._entries

    # ---------------------------------------------------------- http beacon
    def http_poll(self, task_id: str, listener_id: str, sid: str) -> str:
        now = utcnow()
        with self.engine.begin() as conn:
            session = self.repository.session_by_remote(conn, task_id, listener_id, sid)
            if session is None:
                session = self.repository.create_session(
                    conn,
                    task_id=task_id,
                    listener_id=listener_id,
                    kind="http",
                    remote=sid,
                    info={},
                    now=now,
                )
            elif session.status == "dead":
                self.repository.update_session(conn, task_id, session.id, {"status": "active"}, now=now)
            else:
                self.repository.update_session(conn, task_id, session.id, {"last_seen_at": now}, now=now)
            task = self.repository.pop_queued_task(conn, task_id, session.id, now=now)
        return task.command if task is not None else ""

    def http_result(self, task_id: str, listener_id: str, sid: str, output: str) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            session = self.repository.session_by_remote(conn, task_id, listener_id, sid)
            if session is None:
                return
            self.repository.update_session(
                conn, task_id, session.id, {"last_seen_at": now, "status": "active"}, now=now
            )
            sent = conn.execute(
                select(c2_tasks)
                .where(
                    c2_tasks.c.task_id == task_id,
                    c2_tasks.c.session_id == session.id,
                    c2_tasks.c.status == "sent",
                )
                .order_by(c2_tasks.c.id.desc())
                .limit(1)
            ).mappings().one_or_none()
            if sent is None:
                return
            started = _parse_ts(sent["created_at"])
            duration = int((time.time() - started) * 1000) if started else 0
            self.repository.finish_task(
                conn, task_id, sent["id"], ok=True, output=output, duration_ms=duration, now=now
            )

    # ---------------------------------------------------------- tcp
    def _tcp_accept_loop(self, listener, sock: socket.socket) -> None:
        while True:
            try:
                conn_sock, addr = sock.accept()
            except OSError:
                return
            remote = f"{addr[0]}:{addr[1]}"
            now = utcnow()
            with self.engine.begin() as db:
                session = self.repository.create_session(
                    db,
                    task_id=listener.task_id,
                    listener_id=listener.id,
                    kind="tcp",
                    remote=remote,
                    info={},
                    now=now,
                )
            entry: dict = {"conn": conn_sock, "queue": queue.Queue(), "last_byte": time.monotonic(), "alive": True}
            with self._lock:
                self._entries.setdefault(listener.id, {}).setdefault("sessions", {})[session.id] = entry
                self._session_owner[session.id] = listener.id
            threading.Thread(
                target=self._tcp_reader,
                args=(listener.task_id, session.id, entry),
                daemon=True,
                name=f"c2-tcp-read-{session.id}",
            ).start()

    def _tcp_reader(self, task_id: str, session_id: str, entry: dict) -> None:
        conn_sock: socket.socket = entry["conn"]
        conn_sock.settimeout(1)
        while entry["alive"]:
            try:
                chunk = conn_sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            entry["last_byte"] = time.monotonic()
            entry["queue"].put(chunk.decode("utf-8", "replace"))
        entry["alive"] = False
        now = utcnow()
        with self.engine.begin() as db:
            self.repository.update_session(db, task_id, session_id, {"status": "dead"}, now=now)

    def exec_tcp(self, task_id: str, session_id: str, command: str, timeout_seconds: int) -> tuple[bool, str]:
        with self._lock:
            listener_id = self._session_owner.get(session_id)
            entry = self._entries.get(listener_id or "", {}).get("sessions", {}).get(session_id) if listener_id else None
        if entry is None or not entry["alive"]:
            return False, "session 不在线"
        try:
            entry["conn"].sendall(command.encode("utf-8") + b"\n")
        except OSError as exc:
            return False, f"write failed: {exc}"
        deadline = time.monotonic() + timeout_seconds
        chunks: list[str] = []
        last = time.monotonic()
        while time.monotonic() < deadline:
            try:
                chunks.append(entry["queue"].get(timeout=0.2))
                last = time.monotonic()
            except queue.Empty:
                if chunks and time.monotonic() - last > _TCP_IDLE_SECONDS:
                    break
                if not entry["alive"]:
                    break
        if not chunks:
            return False, "超时无输出"
        return True, "".join(chunks)

    # ---------------------------------------------------------- helpers
    def _mark_listener(self, listener, status: str, error: str) -> None:
        self._mark_listener_by_id(listener.id, status, error, task_id=listener.task_id)

    def _mark_listener_by_id(self, listener_id: str, status: str, error: str, *, task_id: str | None = None) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            if task_id is None:
                from axiom.modules.c2.tables import c2_listeners

                task_id = conn.execute(                    select(c2_listeners.c.task_id).where(c2_listeners.c.id == listener_id)
                ).scalar_one_or_none()
            if task_id is None:
                return
            self.repository.update_listener(conn, task_id, listener_id, {"status": status, "error": error}, now=now)

    def _kill_sessions(self, listener_id: str, *, owned_session_ids: list[str] | None) -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(c2_sessions).where(
                    c2_sessions.c.listener_id == listener_id,
                    c2_sessions.c.status == "active",
                )
            ).mappings().all()
            for row in rows:
                conn.execute(
                    c2_sessions.update()
                    .where(c2_sessions.c.task_id == row["task_id"], c2_sessions.c.id == row["id"])
                    .values(status="dead", updated_at=now)
                )


def _parse_ts(value: object) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None
