from __future__ import annotations


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


def test_intervention_is_idempotent_and_consumed_by_active_lease(client, engine) -> None:
    task = _active_explore(client, engine)
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
    # 56118b6 移除了 GET /tasks/{id}/explore-conversation 端点（前端会话 UI 同步下线），
    # 原先针对该端点的事件/脱敏断言随之删除。InterventionService.conversation()
    # 及其 _session_events/_tool_call_detail/_sanitize_tool_arguments 目前已无调用方。


# 原 test_conversation_shows_sanitized_shell_command_and_can_jump_to_latest 已删除：
# 它验证的 GET /tasks/{id}/explore-conversation 端点在 56118b6 随前端会话 UI 一并移除。
# 脱敏逻辑本身仍由 tests/test_browser_broker.py 覆盖。


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
