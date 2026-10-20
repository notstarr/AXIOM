from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import socket
import time
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import Engine

from axiom.modules.tasks.repository import TaskRepository
from axiom.modules.webshell.repository import WebShellRepository
from axiom.modules.webshell.schemas import (
    CMD_PLACEHOLDER,
    CreateWebShell,
    UpdateWebShell,
    WebShellConnection,
    WebShellExecLog,
    WebShellExecResult,
    WebShellFileOp,
    WebShellRequestTemplate,
    WebShellResponseTemplate,
)
from axiom.shared.clock import utcnow
from axiom.shared.database import transaction
from axiom.shared.errors import ConflictError, NotFoundError


_CANARY = {"posix": "whoami && id && pwd", "windows": "whoami & cd"}
# 跨 shell 探针：cmd 展开 %OS%，PowerShell 展开 $env:OS，sh 只展开 $(...)
_PROBE = "echo AXIOM_%OS%_$env:OS_$(echo posix)"
_PROBE_MARK = "AXIOM_"
_PROBE_WINDOWS_MARK = "Windows_NT"


def _shq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _dq(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class WebShellService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.repository = WebShellRepository()
        self.tasks = TaskRepository()

    def list(self, task_id: str, *, include_archived: bool = False) -> list[WebShellConnection]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return self.repository.list(conn, task_id, include_archived=include_archived)

    def get(self, task_id: str, connection_id: str) -> WebShellConnection:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            return self._require_connection(conn, task_id, connection_id)

    def create(
        self,
        task_id: str,
        body: CreateWebShell,
        *,
        actor: str,
        source_intent_id: str | None = None,
    ) -> WebShellConnection:
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id, allow_archived=False)
            connection = self.repository.create(
                conn,
                task_id=task_id,
                name=body.name,
                url=body.url,
                family=body.family,
                request=body.request or WebShellRequestTemplate(),
                response=body.response or WebShellResponseTemplate(),
                remark=body.remark,
                created_by=actor,
                source_intent_id=source_intent_id,
                now=utcnow(),
            )
        return connection

    def update(self, task_id: str, connection_id: str, body: UpdateWebShell) -> WebShellConnection:
        values = body.model_dump(exclude_unset=True)
        if "request" in values and values["request"] is not None:
            values["request"] = WebShellRequestTemplate.model_validate(values["request"]).model_dump_json()
        if "response" in values and values["response"] is not None:
            values["response"] = WebShellResponseTemplate.model_validate(values["response"]).model_dump_json()
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            self._require_connection(conn, task_id, connection_id)
            updated = self.repository.update(conn, task_id, connection_id, values, now=utcnow())
        assert updated is not None
        return updated

    def archive(self, task_id: str, connection_id: str) -> None:
        with transaction(self.engine) as conn:
            self._require_task(conn, task_id)
            self._require_connection(conn, task_id, connection_id)
            self.repository.update(conn, task_id, connection_id, {"archived": True}, now=utcnow())

    def test(self, task_id: str, connection_id: str, *, actor: str) -> WebShellExecResult:
        return self._run(task_id, connection_id, None, actor=actor, canary=True)

    def exec(
        self,
        task_id: str,
        connection_id: str,
        command: str,
        *,
        actor: str,
        intent_id: str | None = None,
        timeout_seconds: int = 30,
    ) -> WebShellExecResult:
        return self._run(
            task_id,
            connection_id,
            command,
            actor=actor,
            intent_id=intent_id,
            timeout_seconds=timeout_seconds,
        )

    def file(self, task_id: str, connection_id: str, body: WebShellFileOp, *, actor: str, intent_id: str | None = None) -> WebShellExecResult:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            connection = self._require_connection(conn, task_id, connection_id)
        connection = self._resolve_family(task_id, connection)
        command = self._file_command(connection, body)
        result = self._run(task_id, connection_id, command, actor=actor, intent_id=intent_id)
        if body.op == "read" and connection.family == "posix" and result.ok:
            result = result.model_copy(update={"output": self._decode_b64_best_effort(result.output)})
        return result

    def logs(self, task_id: str, connection_id: str, limit: int = 50) -> list[WebShellExecLog]:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            self._require_connection(conn, task_id, connection_id)
            return self.repository.logs(conn, task_id, connection_id, limit=limit)

    # ------------------------------------------------------------------
    def _file_command(self, connection: WebShellConnection, body: WebShellFileOp) -> str:
        posix = connection.family == "posix"
        if body.op == "list":
            return f"ls -la -- {_shq(body.path)}" if posix else f"dir /a {_dq(body.path)}"
        if body.op == "read":
            return f"base64 -- {_shq(body.path)}" if posix else f"type {_dq(body.path)}"
        if not posix:
            raise ConflictError("windows 家族暂不支持 file write，请用 exec 自行落盘")
        encoded = base64.b64encode(body.content.encode("utf-8")).decode("ascii")
        return f"printf '%s' {_shq(encoded)} | base64 -d > {_shq(body.path)} && echo AXIOM_WRITE_OK"

    def _run(
        self,
        task_id: str,
        connection_id: str,
        command: str | None,
        *,
        actor: str,
        intent_id: str | None = None,
        timeout_seconds: int = 30,
        canary: bool = False,
    ) -> WebShellExecResult:
        with self.engine.connect() as conn:
            self._require_task(conn, task_id)
            connection = self._require_connection(conn, task_id, connection_id)
            if connection.archived:
                raise ConflictError(f"Archived WebShell cannot be used: {connection_id}")
        started = time.monotonic()
        if canary and connection.family == "auto":
            detected, (ok, output, error) = self._detect_family(connection, timeout_seconds)
            if detected is None and not ok:
                effective = _PROBE
            else:
                if detected is not None:
                    self._persist_family(task_id, connection_id, detected)
                effective = _CANARY[detected or "posix"]
                ok, output, error = self._request(connection, effective, timeout_seconds)
        else:
            effective = command if command is not None else _CANARY[connection.family]
            ok, output, error = self._request(connection, effective, timeout_seconds)
        duration_ms = int((time.monotonic() - started) * 1000)
        checked_at = utcnow()
        status_update = {
            "status": "alive" if ok else "dead",
            "last_check_at": checked_at,
            "last_check_summary": (output or error)[:200],
        }
        with transaction(self.engine) as conn:
            self.repository.update(conn, task_id, connection_id, status_update, now=checked_at)
            log = self.repository.add_log(
                conn,
                task_id=task_id,
                connection_id=connection_id,
                command=effective,
                output=output if ok else (error or output),
                ok=ok,
                duration_ms=duration_ms,
                actor=actor,
                intent_id=intent_id,
                now=checked_at,
            )
        if not ok and canary:
            raise ConflictError(f"WebShell 连接测试失败：{error or output or '无输出'}")
        return WebShellExecResult(
            connection_id=connection_id,
            log_id=log.id,
            command=effective,
            ok=ok,
            output=output if ok else (error or output),
            duration_ms=duration_ms,
            checked_at=checked_at,
        )

    def _resolve_family(self, task_id: str, connection: WebShellConnection) -> WebShellConnection:
        if connection.family != "auto":
            return connection
        detected, _ = self._detect_family(connection, timeout_seconds=30)
        if detected is None:
            raise ConflictError("无法自动判定目标系统（posix/windows），请在连接配置里手动选择")
        self._persist_family(task_id, connection.id, detected)
        return connection.model_copy(update={"family": detected})

    def _persist_family(self, task_id: str, connection_id: str, family: str) -> None:
        with transaction(self.engine) as conn:
            self.repository.update(conn, task_id, connection_id, {"family": family}, now=utcnow())

    @staticmethod
    def _detect_family(
        connection: WebShellConnection, timeout_seconds: int
    ) -> tuple[str | None, tuple[bool, str, str]]:
        outcome = WebShellService._request(connection, _PROBE, timeout_seconds)
        ok, output, _ = outcome
        if not ok or _PROBE_MARK not in output:
            return None, outcome
        return ("windows" if _PROBE_WINDOWS_MARK in output else "posix"), outcome

    @staticmethod
    def _request(connection: WebShellConnection, command: str, timeout_seconds: int) -> tuple[bool, str, str]:
        request = connection.request
        payload = command
        if request.wrap == "base64":
            payload = base64.b64encode(command.encode("utf-8")).decode("ascii")
        encoded = quote(payload, safe="")
        url = connection.url
        if request.query:
            joined = request.query.replace(CMD_PLACEHOLDER, encoded)
            url = f"{url}{'&' if '?' in url else '?'}{joined}"
        body = request.body.replace(CMD_PLACEHOLDER, encoded) if request.body else ""
        WebShellService._assert_safe_target(url)
        try:
            with httpx.Client(timeout=timeout_seconds, verify=request.verify_tls, trust_env=False) as client:
                response = client.request(
                    request.method,
                    url,
                    content=body.encode("utf-8") if request.method == "POST" else None,
                    headers=request.headers,
                )
        except httpx.HTTPError as exc:
            return False, "", f"{type(exc).__name__}: {exc}"
        text = response.content.decode("utf-8", "replace")
        output, extracted = WebShellService._extract(text, connection.response)
        ok = response.status_code == 200 and extracted
        return ok, output, "" if ok else f"HTTP {response.status_code}" + ("" if extracted else "：响应提取失败")

    @staticmethod
    def _assert_safe_target(url: str) -> None:
        host = urlsplit(url).hostname
        if not host:
            raise ConflictError("WebShell URL 缺少主机")
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ConflictError(f"无法解析 WebShell 目标：{exc}") from exc
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                    or ip.is_unspecified or ip.is_reserved):
                raise ConflictError("WebShell 目标地址指向本机/链路本地/保留地址，已拒绝")

    @staticmethod
    def _extract(text: str, template: WebShellResponseTemplate) -> tuple[str, bool]:
        body = text
        if template.decode == "base64":
            decoded = WebShellService._decode_b64_best_effort(text)
            body = decoded if decoded is not None else text
        if template.mode == "raw":
            return body, bool(body.strip())
        if template.mode == "between":
            start = body.find(template.start)
            if start < 0:
                return "", False
            rest = body[start + len(template.start):]
            end = rest.find(template.end)
            if end < 0:
                return "", False
            return rest[:end], True
        match = re.search(template.pattern, body, re.S)
        if match is None:
            return "", False
        try:
            return match.group(template.group), True
        except (IndexError, re.error):
            return "", False

    @staticmethod
    def _decode_b64_best_effort(text: str) -> str | None:
        cleaned = re.sub(r"\s+", "", text)
        if not cleaned:
            return None
        try:
            return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=True).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return None

    def _require_task(self, conn, task_id: str, *, allow_archived: bool = True):
        task = self.tasks.get(conn, task_id)
        if task is None:
            raise NotFoundError(f"Task not found: {task_id}")
        if not allow_archived and task.archived:
            raise ConflictError("已归档任务不能新增 WebShell 连接")
        return task

    def _require_connection(self, conn, task_id: str, connection_id: str) -> WebShellConnection:
        connection = self.repository.get(conn, task_id, connection_id)
        if connection is None:
            raise NotFoundError(f"WebShell connection not found: {connection_id}")
        return connection
