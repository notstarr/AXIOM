from __future__ import annotations


def _task(client, *, enabled: bool) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Guard"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "监管",
            "origin": "a.example.com",
            "guard_enabled": enabled,
        },
    ).json()


def _mode(client, mode: str) -> None:
    current = client.get("/api/v1/settings/runtime").json()
    config = current["config"]
    config["guard_mode"] = mode
    response = client.put(
        "/api/v1/settings/runtime",
        json={"actor": "test", "config": config},
    )
    assert response.status_code == 200, response.text


def _decide(client, task: dict, command: str) -> dict:
    response = client.post(
        "/api/v1/internal/guard/pre",
        json={
            "project_id": task["project_id"],
            "task_id": task["id"],
            "intent_id": "i001",
            "phase": "bootstrap",
            "worker": "worker-a",
            "tool_name": "bash",
            "input": {"command": command},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_guard_is_task_scoped_and_hard_rules_block_only_when_enabled(client) -> None:
    disabled = _task(client, enabled=False)
    assert _decide(client, disabled, "systemctl restart nginx")["source"] == "disabled"

    enabled = _task(client, enabled=True)
    decision = _decide(client, enabled, "systemctl restart nginx")
    assert decision["decision"] == "BLOCK"
    assert decision["source"] == "hard_rule"
    assert decision["event_id"].startswith("evt_")

    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": enabled["id"], "event_type": "guard.decision"},
    ).json()
    assert events[0]["payload"]["decision"] == "BLOCK"


def test_guard_audit_mode_records_block_but_does_not_interrupt_worker(client) -> None:
    task = _task(client, enabled=True)
    _mode(client, "audit")

    decision = _decide(client, task, "rm -rf /etc")

    assert decision["decision"] == "ALLOW"
    assert decision["observed_decision"] == "BLOCK"
    assert decision["observed_reason"]


def test_guard_fails_open_for_normal_external_operation_without_model_config(client) -> None:
    task = _task(client, enabled=True)
    decision = _decide(client, task, "curl https://a.example.com/health")
    assert decision["decision"] == "ALLOW"
    assert decision["source"] == "fail_open"
