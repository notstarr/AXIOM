from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _ShellHandler(BaseHTTPRequestHandler):
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
        if "whoami" in command:
            output = "--BEGIN--\nroot\n--END--"
        elif command.startswith("ls"):
            output = "--BEGIN--\ntotal 0\ndrwxr-xr-x 2 root root 40 .\n--END--"
        else:
            output = "--BEGIN--\nok\n--END--"
        payload = output.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def shell_server():
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


def _create(client, task: dict, url: str) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task['id']}/webshells",
        json={
            "name": "测试 shell",
            "url": url,
            "response": {"mode": "between", "start": "--BEGIN--", "end": "--END--"},
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


def test_webshell_request_template_requires_placeholder(client) -> None:
    task = _task(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/webshells",
        json={"name": "坏模板", "url": "http://127.0.0.1:9/x", "request": {"body": "cmd=id"}},
    )
    assert response.status_code == 422
