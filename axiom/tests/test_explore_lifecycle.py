from __future__ import annotations


BOOTSTRAP = {
    "summary": "完成初始攻击面发现。",
    "artifacts": {
        "asset_inventory": "# 资产\n\n- a.example.com",
        "initial_leads": "# 线索\n\n- 验证对象级授权",
        "findings": "# 已确认问题\n\n- 本轮无确认漏洞",
    },
}


def _task_with_explore_intents(client, *, count: int = 1) -> tuple[dict, list[str]]:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Explore 生命周期",
            "origin": "a.example.com",
            "goal": "验证 Explore 原子收口",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 8},
    )
    bootstrap = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{bootstrap_base}/start", json={"lease_id": bootstrap["lease_id"]})
    completed = client.post(
        f"{bootstrap_base}/bootstrap-complete",
        json={"lease_id": bootstrap["lease_id"], **BOOTSTRAP},
    ).json()
    reason_id = completed["next_intent_id"]
    reason = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{reason_id}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    reason_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{reason_id}"
    client.post(f"{reason_base}/start", json={"lease_id": reason["lease_id"]})
    execution_input = client.post(
        f"{reason_base}/input",
        json={"lease_id": reason["lease_id"]},
    ).json()
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "intent_id": reason_id,
            "lease_id": reason["lease_id"],
            "context_revision": execution_input["context_revision"],
        },
    ).json()
    draft = {
        "todo_adds": [
            {
                "ref": f"todo-{number}",
                "title": f"验证测试方向 {number}",
                "category": "authorization",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
            for number in range(count)
        ],
        "todo_updates": [],
        "intent_proposals": [
            {
                "todo_ref": f"todo-{number}",
                "from_facts": [completed["fact_id"]],
                "description": f"验证接口方向 {number} 的授权边界",
                "targets": ["a.example.com"],
                "execute_seconds": 300,
                "conclude_seconds": 60,
            }
            for number in range(count)
        ],
    }
    result = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/submit",
        json={"lease_id": reason["lease_id"], "draft": draft},
    )
    assert result.status_code == 200, result.text
    return task, result.json()["created_intent_ids"]


def _complete_explore(client, task_id: str, intent_id: str, *, number: int = 1) -> dict:
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    response = client.post(
        f"{base}/explore-complete",
        json={
            "lease_id": claim["lease_id"],
            "output": {
                "fact": f"完成第 {number} 个授权测试，确认接口返回行为并保存证据。",
                "artifacts": [
                    {
                        "filename": f"evidence-{number}.md",
                        "artifact_type": "explore.http_evidence",
                        "summary": "授权测试请求与响应证据",
                        "content": f"# 证据 {number}\n\nGET /api/orders/{number}",
                    }
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_explore_completion_atomically_registers_artifact_fact_and_reason(client, data_dir) -> None:
    task, intents = _task_with_explore_intents(client)

    completed = _complete_explore(client, task["id"], intents[0])

    assert completed["fact_id"] == "f002"
    assert completed["reason_intent_id"] == "i004"
    assert completed["artifacts"][0]["artifact_type"] == "explore.http_evidence"
    artifact = completed["artifacts"][0]
    assert (data_dir / artifact["storage_key"]).read_text(encoding="utf-8").startswith("# 证据")
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    explore = next(item for item in graph["intents"] if item["id"] == intents[0])
    reason = next(item for item in graph["intents"] if item["id"] == "i004")
    assert explore["status"] == "completed"
    assert explore["to_fact_id"] == "f002"
    assert reason["from_facts"] == ["f002"]
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"


def test_reason_trigger_batches_new_facts_while_explore_backlog_remains(client) -> None:
    task, intent_ids = _task_with_explore_intents(client, count=4)

    first = _complete_explore(client, task["id"], intent_ids[0], number=1)
    assert first["reason_intent_id"] is None
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "explore"

    second = _complete_explore(client, task["id"], intent_ids[1], number=2)
    assert second["reason_intent_id"] == "i007"
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    reason = next(item for item in graph["intents"] if item["id"] == "i007")
    assert reason["from_facts"] == ["f002", "f003"]


def test_explore_completion_is_idempotent_for_the_same_lease(client) -> None:
    task, intents = _task_with_explore_intents(client)
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    payload = {
        "lease_id": claim["lease_id"],
        "output": {
            "fact": "完成授权测试并保存证据。",
            "artifacts": [
                {
                    "filename": "evidence.md",
                    "artifact_type": "explore.evidence",
                    "summary": "测试证据",
                    "content": "# 证据\n\n完整内容",
                }
            ],
        },
    }

    first = client.post(f"{base}/explore-complete", json=payload)
    second = client.post(f"{base}/explore-complete", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(
        [
            item
            for item in client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()
            if item["intent_id"] == intents[0]
        ]
    ) == 1
