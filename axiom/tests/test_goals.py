from __future__ import annotations

import yaml

from axiom.modules.goals.tables import goal_evaluations
from protocol_helpers import (
    apply_reason_draft,
    bootstrap_files_from_legacy,
    claim_reason_run,
    complete_bootstrap,
)

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
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap["lease_id"],
        files=bootstrap_files_from_legacy(BOOTSTRAP),
    )
    reason, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
        worker_name="worker-a",
    )
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


def test_human_goal_completion_cancels_reason_run_and_abandons_workspace(client) -> None:
    task, reason_claim, completed = _active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()

    response = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "人工复核确认 Goal 已由现有 Fact 满足",
            "evidence_refs": [f"fact:{completed['fact_id']}"],
            "evaluator": "human",
        },
    )

    assert response.status_code == 201, response.text
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_claim['reason_run_id']}"
    ).json()
    assert run["status"] == "cancelled"
    assert run["lease_id"] is None
    abandoned = client.get(f"/api/v1/reason-workspaces/{workspace['id']}").json()
    assert abandoned["status"] == "abandoned"
    assert abandoned["failure_reason"] == "goal_achieved"
    stale_commit = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": reason_claim["lease_id"]},
    )
    assert stale_commit.status_code == 409


def test_legacy_achieved_goal_without_persisted_evidence_remains_readable(
    client,
    engine,
) -> None:
    project = client.post("/api/v1/projects", json={"title": "Legacy Goal"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "Legacy Goal", "origin": "a.example.com"},
    ).json()
    with engine.begin() as conn:
        conn.execute(
            goal_evaluations.insert().values(
                id="legacy_goal_test",
                task_id=task["id"],
                intent_id=None,
                workspace_id=None,
                achieved=True,
                reason="Legacy Task was marked completed; evidence was not persisted.",
                evidence_refs_json="[]",
                evaluator="migration.legacy",
                created_at="2026-07-24T00:00:00Z",
            )
        )

    response = client.get(f"/api/v1/tasks/{task['id']}/goal-evaluations")

    assert response.status_code == 200, response.text
    assert response.json()[0]["achieved"] is True
    assert response.json()[0]["evidence_refs"] == []


def test_reason_can_complete_goal_atomically_without_dispatching_more_work(client) -> None:
    task, reason_claim, completed = _active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        {
                "todo_adds": [],
                "todo_updates": [],
                "intent_proposals": [],
                "goal_assessment": {
                    "achieved": True,
                    "reason": "Bootstrap 已覆盖目标系统，现有证据满足本轮 Goal。",
                    "evidence_refs": [f"fact:{completed['fact_id']}"],
                },
        },
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/reason-workspaces/{workspace['id']}").json()[
        "status"
    ] == "committed"
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "completed"
    evaluations = client.get(f"/api/v1/tasks/{task['id']}/goal-evaluations").json()
    assert evaluations[0]["achieved"] is True
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert [item["id"] for item in graph["intents"]] == ["i001"]
    assert graph["goal_from_fact_ids"] == [completed["fact_id"]]
    assert all("goal" not in item["from_facts"] for item in graph["intents"])
    overview = yaml.safe_load(
        client.get(f"/api/v1/tasks/{task['id']}/export?format=yaml").text
    )
    assert [item["id"] for item in overview["intents"]] == ["i001"]
    assert overview["goal_evaluations"][0]["reason_run_id"] == reason_claim["reason_run_id"]
    assert overview["goal_evaluations"][0]["evidence_refs"] == [
        f"fact:{completed['fact_id']}"
    ]


def test_reason_goal_completion_requires_at_least_one_fact_evidence(client) -> None:
    task, reason_claim, _completed = _active_reason(client)
    artifact = client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()[0]
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()

    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        {
                "goal_assessment": {
                    "achieved": True,
                    "reason": "只有产物引用，尚未提供完成关系所需的 Fact 来源",
                    "evidence_refs": [f"artifact:{artifact['id']}"],
                }
        },
    )

    assert response.status_code == 409
    assert "at least one Fact evidence ref" in response.json()["error"]["message"]
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "running"


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
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        {
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
                        "conclude_seconds": 180,
                    }
                ],
                "goal_assessment": {
                    "achieved": False,
                    "reason": "Hint 要求的独立复核尚未完成",
                    "evidence_refs": [f"hint:{hint['id']}"],
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


def test_goal_fact_cannot_prove_itself(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Goal Self Reference"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Goal Self Reference",
            "origin": "a.example.com",
        },
    ).json()

    response = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "错误地使用 Goal 自身证明 Goal",
            "evidence_refs": ["fact:goal"],
        },
    )

    assert response.status_code == 409
    assert "cannot be used as Goal evidence" in response.json()["error"]["message"]


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
