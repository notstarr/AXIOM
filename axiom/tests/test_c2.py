from __future__ import annotations

import base64
import socket
import threading
import time
import urllib.error
import urllib.request

import click
import pytest

from axiom.modules.c2.schemas import C2ExecCommand
from axiom.modules.c2.token import c2_listener_token


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _red_team_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "c2 测试"}).json()
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "红队 c2",
            "origin": "c2.example.com",
            "engagement_mode": "red_team",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_listener(client, task: dict, kind: str, port: int) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task['id']}/c2/listeners",
        json={"name": f"{kind} 监听", "kind": kind, "host": "127.0.0.1", "port": port},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_c2_http_listener_beacon_roundtrip(client) -> None:
    task = _red_team_task(client)
    port = _free_port()
    listener = _create_listener(client, task, "http", port)
    started = client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/start")
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "listening"

    token = c2_listener_token(listener["id"])
    unauthorized = f"http://127.0.0.1:{port}/index?sid=beacon1"
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(unauthorized, timeout=5)
    assert exc.value.code == 403
    exc.value.close()

    base = f"{unauthorized}&token={token}"
    with urllib.request.urlopen(base, timeout=5) as response:
        assert response.read() == b""
    sessions = client.get(f"/api/v1/tasks/{task['id']}/c2/sessions").json()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["status"] == "active"

    container = client.app.state.container
    holder: list = []
    thread = threading.Thread(
        target=lambda: holder.append(
            container.c2.exec(
                task["id"],
                session["id"],
                C2ExecCommand(command="id", wait_seconds=10),
                actor="pytest",
            )
        ),
    )
    thread.start()
    command = ""
    for _ in range(50):
        with urllib.request.urlopen(base, timeout=5) as response:
            command = response.read().decode("utf-8")
        if command:
            break
        time.sleep(0.2)
    assert command == "id"
    payload = base64.b64encode(b"uid=0(root)\n").decode("ascii")
    request = urllib.request.Request(base, data=payload.encode("utf-8"), method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
    thread.join(timeout=10)
    assert not thread.is_alive()
    result = holder[0]
    assert result.status == "completed"
    assert result.output == "uid=0(root)\n"

    stopped = client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/stop")
    assert stopped.status_code == 200
    sessions = client.get(f"/api/v1/tasks/{task['id']}/c2/sessions").json()
    assert sessions[0]["status"] == "dead"


def test_c2_tcp_listener_exec(client) -> None:
    task = _red_team_task(client)
    port = _free_port()
    listener = _create_listener(client, task, "tcp", port)
    started = client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/start")
    assert started.json()["status"] == "listening"

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    deadline = time.monotonic() + 5
    sessions = []
    while time.monotonic() < deadline:
        sessions = client.get(f"/api/v1/tasks/{task['id']}/c2/sessions").json()
        if sessions:
            break
        time.sleep(0.2)
    assert sessions, "tcp 会话未上线"
    session = sessions[0]

    container = client.app.state.container
    holder: list = []
    thread = threading.Thread(
        target=lambda: holder.append(
            container.c2.exec(
                task["id"],
                session["id"],
                C2ExecCommand(command="whoami", wait_seconds=8),
                actor="pytest",
            )
        ),
    )
    thread.start()
    sock.settimeout(5)
    received = sock.recv(4096).decode("utf-8")
    assert received.strip() == "whoami"
    sock.sendall(b"uid=1(worker)\n")
    thread.join(timeout=12)
    assert not thread.is_alive()
    result = holder[0]
    assert result.status == "completed"
    assert "uid=1(worker)" in result.output
    sock.close()
    client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/stop")


def test_c2_exec_without_beacon_times_out_as_sent(client) -> None:
    task = _red_team_task(client)
    port = _free_port()
    listener = _create_listener(client, task, "http", port)
    client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/start")
    token = c2_listener_token(listener["id"])
    base = f"http://127.0.0.1:{port}/index?sid=lonely&token={token}"
    with urllib.request.urlopen(base, timeout=5) as response:
        assert response.read() == b""
    session = client.get(f"/api/v1/tasks/{task['id']}/c2/sessions").json()[0]
    response = client.post(
        f"/api/v1/tasks/{task['id']}/c2/sessions/{session['id']}/exec",
        json={"command": "id", "wait_seconds": 1},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/stop")


def test_c2_oneliner_and_validation(client) -> None:
    task = _red_team_task(client)
    listener = _create_listener(client, task, "http", _free_port())
    oneliner = client.get(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/oneliner").json()
    token = c2_listener_token(listener["id"])
    assert "sid=" in oneliner["bash"]
    assert "token=" in oneliner["bash"]
    assert token in oneliner["bash"]
    assert token in oneliner["python"]
    assert "sleep" in oneliner["bash"]

    missing = client.post(
        "/api/v1/tasks/t_404/c2/listeners",
        json={"name": "x", "kind": "http", "host": "127.0.0.1", "port": 4444},
    )
    assert missing.status_code == 404

    project = client.post("/api/v1/projects", json={"title": "c2 vuln"}).json()
    vuln_task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "漏洞任务", "origin": "a.example.com"},
    ).json()
    blocked = client.post(
        f"/api/v1/tasks/{vuln_task['id']}/c2/listeners",
        json={"name": "x", "kind": "http", "host": "127.0.0.1", "port": _free_port()},
    )
    assert blocked.status_code == 201  # REST 不限制模式，门禁在 worker CLI 侧
    client.post(f"/api/v1/tasks/{vuln_task['id']}/stop")
    client.post(f"/api/v1/tasks/{vuln_task['id']}/archive")
    archived = client.post(
        f"/api/v1/tasks/{vuln_task['id']}/c2/listeners",
        json={"name": "y", "kind": "http", "host": "127.0.0.1", "port": _free_port()},
    )
    assert archived.status_code == 409


def test_worker_cli_red_team_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from axiom import worker_cli

    monkeypatch.setenv("AXIOM_TASK_ID", "t_100")
    calls: list = []

    def fake_request(method: str, path: str, payload):
        calls.append((method, path))
        return {"engagement_mode": "vulnerability"}

    monkeypatch.setattr(worker_cli, "_request", fake_request)
    with pytest.raises(click.ClickException, match="红队模式"):
        worker_cli._red_team_task_id()

    monkeypatch.setattr(worker_cli, "_request", lambda *a, **k: {"engagement_mode": "red_team"})
    worker_cli._RED_TEAM_MODE_CACHE.clear()
    assert worker_cli._red_team_task_id() == "t_100"
