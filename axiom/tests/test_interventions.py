from __future__ import annotations

import json
from pathlib import Path

from axiom.modules.graph.tables import intents


def _active_explore(client, engine):
    project = client.post("/api/v1/projects", json={"title": "对话测试"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "实时 Explore",
            "origin": "https://example.test",
            "goal": "验证对话链路",
            "worker_pool": ["worker-a"],
        },
    ).json()
    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(kind="explore", status="running", lease_id="lease-test", worker="worker-a")
        )
    return task


def test_intervention_is_idempotent_and_consumed_by_active_lease(client, engine, data_dir) -> None:
    task = _active_explore(client, engine)
    run_dir = Path(data_dir) / "runs" / task["id"] / "i001" / "lease-test" / "sessions"
    run_dir.mkdir(parents=True)
    (run_dir / "session.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "message", "timestamp": "2026-08-04T01:00:00Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "browser", "arguments": {"authorization": "secret"}}]}}),
                json.dumps({"type": "message", "timestamp": "2026-08-04T01:00:01Z", "message": {"role": "toolResult", "toolName": "browser", "content": [{"type": "text", "text": "authorization: Bearer secret"}]}}),
            ]
        ),
        encoding="utf-8",
    )
    payload = {"message": "先确认当前接口，再补充 findings.md", "client_message_id": "client-1", "actor": "human.frontend"}
    first = client.post(f"/api/v1/tasks/{task['id']}/intents/i001/interventions", json=payload)
    assert first.status_code == 200, first.text
    second = client.post(f"/api/v1/tasks/{task['id']}/intents/i001/interventions", json=payload)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    claimed = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/interventions/claim",
        json={"lease_id": "lease-test"},
    )
    assert claimed.status_code == 200
    assert claimed.json()[0]["status"] == "sent"
    intervention_id = claimed.json()[0]["id"]
    resolved = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/interventions/{intervention_id}/resolve?lease_id=lease-test",
        json={"success": True},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "accepted"

    conversation = client.get(
        f"/api/v1/tasks/{task['id']}/explore-conversation?intent_id=i001"
    )
    assert conversation.status_code == 200
    events = conversation.json()["events"]
    assert any(item["kind"] == "intervention" for item in events)
    assert any(item["kind"] == "tool_call" for item in events)
    assert all("Bearer secret" not in item.get("detail", "") for item in events)
    tool_call = next(item for item in events if item["kind"] == "tool_call")
    assert tool_call["tool"] == "browser"
    assert "[已脱敏]" in tool_call["detail"]
    assert "secret" not in tool_call["detail"]


def test_conversation_shows_sanitized_shell_command_and_can_jump_to_latest(client, engine, data_dir) -> None:
    task = _active_explore(client, engine)
    run_dir = Path(data_dir) / "runs" / task["id"] / "i001" / "lease-test" / "sessions"
    run_dir.mkdir(parents=True)
    (run_dir / "session.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "message", "timestamp": "2026-08-04T01:00:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "第一步"}]}}),
                json.dumps({"type": "message", "timestamp": "2026-08-04T01:00:01Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "第二步"}]}}),
                json.dumps({"type": "message", "timestamp": "2026-08-04T01:00:02Z", "message": {"role": "assistant", "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "curl -H 'Authorization: Bearer top-secret' https://example.test/api"}}]}}),
            ]
        ),
        encoding="utf-8",
    )

    response = client.get(
        f"/api/v1/tasks/{task['id']}/explore-conversation?intent_id=i001&latest=true&limit=1"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_previous"] is True
    assert payload["has_more"] is False
    assert len(payload["events"]) == 1
    event = payload["events"][0]
    assert event["kind"] == "tool_call"
    assert event["tool"] == "bash"
    assert "curl" in event["detail"]
    assert "https://example.test/api" in event["detail"]
    assert "top-secret" not in event["detail"]
    assert "[已脱敏]" in event["detail"]


def test_intervention_requires_active_explore_lease(client, engine) -> None:
    task = _active_explore(client, engine)
    with engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(status="completed", lease_id=None)
        )
    response = client.post(
        f"/api/v1/tasks/{task['id']}/intents/i001/interventions",
        json={"message": "继续", "client_message_id": "client-2"},
    )
    assert response.status_code == 409
