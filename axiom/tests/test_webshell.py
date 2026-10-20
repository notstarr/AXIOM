from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _ShellHandler(BaseHTTPRequestHandler):
    commands: list[str] = []

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def _cmd(self) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        from urllib.parse import parse_qs, unquote, urlparse

        if body:
            return unquote(parse_qs(body).get("cmd", [""])[0])
        return unquote(parse_qs(urlparse(self.path).query).get("cmd", [""])[0])

    def do_POST(self) -> None:  # noqa: N802
        command = self._cmd()
        type(self).commands.append(command)
        windows = "/win" in self.path
        if command.startswith("echo AXIOM_"):
            output = "AXIOM_Windows_NT_$env:OS_$(echo posix)" if windows else "AXIOM_%OS%_:OS_posix"
        elif "whoami" in command:
            output = "nt authority\\system" if windows else "root"
        elif command.startswith("ls"):
            output = "total 0\ndrwxr-xr-x 2 root root 40 ."
        elif command.startswith("dir /a"):
            output = " Volume in drive C has no label."
        else:
            output = "ok"
        payload = f"--BEGIN--\n{output}\n--END--".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def shell_server():
    _ShellHandler.commands = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ShellHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/shell"
    httpd.shutdown()


def _task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "webshell 测试"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "webshell 任务",
            "origin": "shell.example.com",
        },
    ).json()


def _create(client, task: dict, url: str, **extra) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task['id']}/webshells",
        json={
            "name": "测试 shell",
            "url": url,
            "response": {"mode": "between", "start": "--BEGIN--", "end": "--END--"},
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_webshell_crud_and_exec_roundtrip(client, shell_server) -> None:
    task = _task(client)
    connection = _create(client, task, shell_server)
    assert connection["status"] == "untested"
    assert connection["task_id"] == task["id"]

    tested = client.post(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/test")
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["ok"] is True
    assert body["output"].strip() == "root"

    listed = client.get(f"/api/v1/tasks/{task['id']}/webshells").json()
    assert listed[0]["status"] == "alive"

    executed = client.post(
        f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/exec",
        json={"command": "ls -la /tmp"},
    )
    assert executed.status_code == 200, executed.text
    assert "total 0" in executed.json()["output"]

    files = client.post(
        f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/file",
        json={"op": "list", "path": "/tmp"},
    )
    assert files.status_code == 200, files.status_code
    logs = client.get(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/logs").json()
    assert logs[0]["command"] == "ls -la -- '/tmp'"

    archived = client.delete(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}")
    assert archived.status_code == 204
    assert client.get(f"/api/v1/tasks/{task['id']}/webshells").json() == []


def test_webshell_exec_failure_marks_dead(client) -> None:
    task = _task(client)
    connection = _create(client, task, "http://127.0.0.1:9/nope")
    response = client.post(
        f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/exec",
        json={"command": "id"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    listed = client.get(f"/api/v1/tasks/{task['id']}/webshells").json()
    assert listed[0]["status"] == "dead"


def test_webshell_requires_task_and_rejects_archived(client, shell_server) -> None:
    task = _task(client)
    missing = client.post(
        "/api/v1/tasks/t_404/webshells",
        json={"name": "x", "url": shell_server},
    )
    assert missing.status_code == 404

    connection = _create(client, task, shell_server)
    client.post(f"/api/v1/tasks/{task['id']}/stop")
    archived_task = client.post(f"/api/v1/tasks/{task['id']}/archive")
    assert archived_task.status_code in (200, 204)
    blocked = client.post(
        f"/api/v1/tasks/{task['id']}/webshells",
        json={"name": "y", "url": shell_server},
    )
    assert blocked.status_code == 409


def test_webshell_auto_detects_posix_and_persists(client, shell_server) -> None:
    task = _task(client)
    connection = _create(client, task, shell_server)
    assert connection["family"] == "auto"

    tested = client.post(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["output"].strip() == "root"
    detail = client.get(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}").json()
    assert detail["family"] == "posix"
    assert _ShellHandler.commands[0].startswith("echo AXIOM_")
    assert _ShellHandler.commands[1] == "whoami && id && pwd"

    client.post(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/test")
    probes = [c for c in _ShellHandler.commands if c.startswith("echo AXIOM_")]
    assert probes == [_ShellHandler.commands[0]]


def test_webshell_auto_detects_windows_and_file_ops(client, shell_server) -> None:
    task = _task(client)
    connection = _create(client, task, f"{shell_server}/win")

    tested = client.post(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/test")
    assert tested.status_code == 200, tested.text
    assert "nt authority" in tested.json()["output"]
    detail = client.get(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}").json()
    assert detail["family"] == "windows"

    listed = client.post(
        f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/file",
        json={"op": "list", "path": "C:\\inetpub"},
    )
    assert listed.status_code == 200, listed.text
    logs = client.get(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/logs").json()
    assert logs[0]["command"] == 'dir /a "C:\\inetpub"'

    blocked = client.post(
        f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/file",
        json={"op": "write", "path": "C:\\x", "content": "y"},
    )
    assert blocked.status_code == 409


def test_webshell_manual_family_skips_probe(client, shell_server) -> None:
    task = _task(client)
    connection = _create(client, task, shell_server, family="posix")
    assert connection["family"] == "posix"

    tested = client.post(f"/api/v1/tasks/{task['id']}/webshells/{connection['id']}/test")
    assert tested.status_code == 200, tested.text
    assert _ShellHandler.commands == ["whoami && id && pwd"]


def test_webshell_request_template_requires_placeholder(client) -> None:
    task = _task(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/webshells",
        json={"name": "坏模板", "url": "http://127.0.0.1:9/x", "request": {"body": "cmd=id"}},
    )
    assert response.status_code == 422
