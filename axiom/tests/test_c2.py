from __future__ import annotations

import base64
import socket
import threading
import time
import urllib.error
import urllib.request

import click
import pytest
from sqlalchemy import text as sa_text

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


def test_c2_tcp_listener_is_refused(client) -> None:
    """TCP has no handshake and no token, so creating one must fail."""

    task = _red_team_task(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/c2/listeners",
        json={"name": "tcp 监听", "kind": "tcp", "host": "127.0.0.1", "port": _free_port()},
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/api/v1/tasks/{task['id']}/c2/listeners").json() == []


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.192.192.205", "example.com"])
def test_c2_listener_must_bind_loopback(client, host: str) -> None:
    """A public bind address is what put listeners on 0.0.0.0:443 before."""

    task = _red_team_task(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/c2/listeners",
        json={"name": "公网监听", "kind": "http", "host": host, "port": _free_port()},
    )
    assert response.status_code == 422, response.text


def test_c2_listener_start_rejects_legacy_tcp_row(client) -> None:
    """Rows created before TCP was retired must not start either."""

    task = _red_team_task(client)
    listener = _create_listener(client, task, "http", _free_port())
    container = client.app.state.container
    with container.c2.engine.begin() as conn:
        conn.execute(
            sa_text(
                "UPDATE axiom.c2_listeners SET kind='tcp' "
                "WHERE task_id=:task_id AND id=:listener_id"
            ),
            {"task_id": task["id"], "listener_id": listener["id"]},
        )
    response = client.post(
        f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/start"
    )
    assert response.status_code == 422, response.text
    assert "停用" in response.text


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


def test_oneliner_uses_public_base_when_configured(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """A beacon must carry the Caddy-published URL, not the loopback bind."""

    task = _red_team_task(client)
    port = _free_port()
    listener = _create_listener(client, task, "http", port)
    url = f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/oneliner"

    monkeypatch.delenv("AXIOM_C2_PUBLIC_BASE", raising=False)
    fallback = client.get(url).json()
    assert f"http://127.0.0.1:{port}/index" in fallback["bash"]
    assert "AXIOM_C2_PUBLIC_BASE" in fallback["note"]

    monkeypatch.setenv("AXIOM_C2_PUBLIC_BASE", "https://203.0.113.10:8888/c2/")
    public = client.get(url).json()
    assert "https://203.0.113.10:8888/c2/index" in public["bash"]
    assert "https://203.0.113.10:8888/c2/index" in public["python"]
    assert "127.0.0.1" not in public["bash"]
    assert "警告" not in public["note"]


def test_resume_active_rebinds_crashed_listener(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listener left at ``listening`` by a crash comes back on startup."""

    from axiom.modules.c2.listeners import ListenerManager

    task = _red_team_task(client)
    listener = _create_listener(client, task, "http", _free_port())
    client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/start")
    client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/stop")

    container = client.app.state.container
    row = client.get(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}").json()
    assert row["status"] == "stopped"

    with container.engine.begin() as conn:
        conn.execute(
            sa_text("update axiom.c2_listeners set status = 'listening' where task_id = :t and id = :i"),
            {"t": task["id"], "i": listener["id"]},
        )
    # A fresh manager stands in for the restarted process.
    container.c2.manager = ListenerManager(container.engine)
    resumed = container.c2.resume_active()
    assert listener["id"] in resumed
    assert client.get(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}").json()["status"] == "listening"
    container.c2.manager.stop_all()


def test_resume_active_skips_legacy_wildcard_bind(client) -> None:
    """Legacy ``0.0.0.0`` rows are marked error instead of blocking the rest."""

    from axiom.modules.c2.listeners import ListenerManager

    task = _red_team_task(client)
    listener = _create_listener(client, task, "http", _free_port())
    container = client.app.state.container
    with container.engine.begin() as conn:
        conn.execute(
            sa_text(
                "update axiom.c2_listeners set status = 'listening', host = '0.0.0.0'"
                " where task_id = :t and id = :i"
            ),
            {"t": task["id"], "i": listener["id"]},
        )
    container.c2.manager = ListenerManager(container.engine)
    assert container.c2.resume_active() == []
    row = client.get(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}").json()
    assert row["status"] == "error"
    assert "回环" in row["error"]


def test_shutdown_keeps_listener_armed_for_next_start(client) -> None:
    """A deploy must not turn a live listener into an operator-initiated stop."""

    from axiom.modules.c2.listeners import ListenerManager

    task = _red_team_task(client)
    listener = _create_listener(client, task, "http", _free_port())
    client.post(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}/start")
    container = client.app.state.container

    container.c2.manager.stop_all()  # what FastAPI shutdown does
    # The API reports the live socket, so during the gap it reads "stopped";
    # what matters is that the stored intent stayed "listening".
    with container.engine.connect() as conn:
        stored = conn.execute(
            sa_text("select status from axiom.c2_listeners where task_id = :t and id = :i"),
            {"t": task["id"], "i": listener["id"]},
        ).scalar_one()
    assert stored == "listening"

    container.c2.manager = ListenerManager(container.engine)  # the restarted process
    assert listener["id"] in container.c2.resume_active()
    assert client.get(f"/api/v1/tasks/{task['id']}/c2/listeners/{listener['id']}").json()["status"] == "listening"
    container.c2.manager.stop_all()
