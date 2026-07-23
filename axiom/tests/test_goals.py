from __future__ import annotations

BOOTSTRAP = {
    "summary": "完成初始攻击面发现。",
    "artifacts": {
        "asset_inventory": "# 资产\n\n- a.example.com",
        "initial_leads": "# 线索\n\n- 已完成目标覆盖",
        "findings": "# 发现\n\n- 已保存证据",
    },
}


def _active_reason(client) -> tuple[dict, dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "Reason Goal"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Reason Goal",
            "origin": "a.example.com",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason"], "max_running": 1},
    )
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    bootstrap = client.post(f"{base}/claim", json={"worker_name": "worker-a"}).json()
    client.post(f"{base}/start", json={"lease_id": bootstrap["lease_id"]})
    completed = client.post(
        f"{base}/bootstrap-complete",
        json={"lease_id": bootstrap["lease_id"], **BOOTSTRAP},
    ).json()
    reason_id = completed["next_intent_id"]
    reason_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{reason_id}"
    reason = client.post(f"{reason_base}/claim", json={"worker_name": "worker-a"}).json()
    client.post(f"{reason_base}/start", json={"lease_id": reason["lease_id"]})
    execution = client.post(
        f"{reason_base}/input",
        json={"lease_id": reason["lease_id"]},
    ).json()
    reason["context_revision"] = execution["context_revision"]
    return task, reason, completed


def test_human_goal_assessment_is_the_only_task_completion_path(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Goal"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "完成标准", "origin": "a.example.com"},
    ).json()

    response = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "已覆盖目标系统并确认目标证据",
            "evidence_refs": ["fact:origin"],
            "evaluator": "human",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["achieved"] is True
    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert current["status"] == "completed"
    assert current["phase"] == "none"
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert graph["intents"][0]["status"] == "cancelled"


def test_reason_can_complete_goal_atomically_without_dispatching_more_work(client) -> None:
    task, reason_claim, completed = _active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "intent_id": reason_claim["intent_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/submit",
        json={
            "lease_id": reason_claim["lease_id"],
            "draft": {
                "todo_adds": [],
                "todo_updates": [],
                "intent_proposals": [],
                "vulnerability_reports": [],
                "goal_assessment": {
                    "achieved": True,
                    "reason": "Bootstrap 已覆盖目标系统，现有证据满足本轮 Goal。",
                    "evidence_refs": [f"fact:{completed['fact_id']}"],
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "completed"
    evaluations = client.get(f"/api/v1/tasks/{task['id']}/goal-evaluations").json()
    assert evaluations[0]["achieved"] is True


def test_goal_evidence_must_belong_to_the_same_task(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Goal"}).json()
    first = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "一", "origin": "a.example.com"},
    ).json()
    second = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "二", "origin": "b.example.com"},
    ).json()
    vulnerability = client.post(
        f"/api/v1/tasks/{first['id']}/vulnerabilities",
        json={
            "title": "任务一漏洞",
            "type": "测试",
            "severity": "low",
            "description": "仅属于任务一",
            "impact": "测试影响",
            "source": "human",
            "reported_by": "human",
        },
    ).json()
    response = client.post(
        f"/api/v1/tasks/{second['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "错误引用测试",
            "evidence_refs": [f"vulnerability:{vulnerability['id']}"],
        },
    )
    assert response.status_code == 409
    assert client.get(f"/api/v1/tasks/{first['id']}").json()["status"] == "running"


def test_unachieved_goal_may_reference_a_task_hint(client) -> None:
    task, reason_claim, completed = _active_reason(client)
    hint = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "必须先由 Explore 独立复核"},
    ).json()
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "intent_id": reason_claim["intent_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/submit",
        json={
            "lease_id": reason_claim["lease_id"],
            "draft": {
                "todo_adds": [
                    {
                        "ref": "verify",
                        "title": "独立复核",
                        "category": "authorization",
                        "sources": [{"type": "hint", "id": hint["id"]}],
                    }
                ],
                "intent_proposals": [
                    {
                        "todo_ref": "verify",
                        "from_facts": [completed["fact_id"]],
                        "description": "按 Hint 独立复核",
                        "targets": ["a.example.com"],
                        "execute_seconds": 300,
                        "conclude_seconds": 60,
                    }
                ],
                "goal_assessment": {
                    "achieved": False,
                    "reason": "Hint 要求的独立复核尚未完成",
                    "evidence_refs": [f"hint:{hint['id']}"],
                },
            },
        },
    )

    assert response.status_code == 200, response.text


def test_task_hint_alone_cannot_prove_an_achieved_goal(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Goal Hint"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Goal Hint",
            "origin": "a.example.com",
            "hints": ["完成所有验证"],
        },
    ).json()

    response = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "错误地把操作要求当成完成证据",
            "evidence_refs": ["hint:h001"],
        },
    )

    assert response.status_code == 409
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "running"


def test_goal_evaluation_ids_are_globally_unique_across_tasks(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Global Goal IDs"}).json()
    tasks = [
        client.post(
            "/api/v1/tasks",
            json={
                "project_id": project["id"],
                "title": title,
                "origin": origin,
            },
        ).json()
        for title, origin in (("一", "a.example.com"), ("二", "b.example.com"))
    ]

    evaluations = []
    for task in tasks:
        response = client.post(
            f"/api/v1/tasks/{task['id']}/goal-evaluations",
            json={
                "achieved": False,
                "reason": "当前证据尚不足以完成 Goal",
                "evidence_refs": ["fact:origin"],
            },
        )
        assert response.status_code == 201, response.text
        evaluations.append(response.json())

    assert [item["id"] for item in evaluations] == ["ge001", "ge002"]
