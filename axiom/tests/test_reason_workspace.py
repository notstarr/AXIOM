from __future__ import annotations

import json

from protocol_helpers import (
    apply_reason_draft,
    bootstrap_files_from_legacy,
    complete_bootstrap,
    claim_reason_run,
)

BOOTSTRAP_PAYLOAD = {
    "summary": "完成初始资产发现。",
    "artifacts": {
        "asset_inventory": "# 资产\n\n- a.example.com",
        "initial_leads": "# 线索\n\n- 验证对象级授权",
        "findings": "# 已确认问题\n\n- 本轮无确认漏洞",
    },
}


def active_reason(client, *, origin: str = "a.example.com") -> tuple[dict, dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "测试",
            "origin": origin,
            "goal": "挖掘高置信度漏洞",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 1},
    )
    bootstrap_claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{bootstrap_base}/start", json={"lease_id": bootstrap_claim["lease_id"]})
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_claim["lease_id"],
        files=bootstrap_files_from_legacy(BOOTSTRAP_PAYLOAD),
    )
    reason_claim, _execution_input = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
        worker_name="worker-a",
    )
    return task, reason_claim, completed


def test_reason_workspace_commits_todos_and_pending_intents_atomically(client, data_dir) -> None:
    task, reason_claim, completed = active_reason(client)
    opened = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    )
    assert opened.status_code == 200, opened.text
    workspace = opened.json()
    draft = {
        "todo_adds": [
            {
                "ref": "authz",
                "title": "验证对象级授权",
                "category": "authorization",
                "theme": "IDOR",
                "priority": 90,
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "todo_updates": [],
        "intent_proposals": [
            {
                "todo_ref": "authz",
                "from_facts": [completed["fact_id"]],
                "description": "验证订单详情接口是否存在对象级授权问题",
                "targets": ["a.example.com"],
                "execute_seconds": 420,
                "conclude_seconds": 180,
            }
        ],
        "goal_assessment": {
            "achieved": False,
            "reason": "该计划项仍需 Explore 独立验证",
            "evidence_refs": ["todo:authz"],
        },
    }

    committed = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        draft,
    )
    assert committed.status_code == 200, committed.text
    result = committed.json()
    assert result["created_todo_ids"] == ["td001"]
    assert result["created_intent_ids"] == ["i002"]

    todo = client.get(f"/api/v1/tasks/{task['id']}/todos").json()[0]
    assert todo["status"] == "active"
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert [item["id"] for item in graph["intents"]] == ["i001", "i002"]
    explore = next(item for item in graph["intents"] if item["id"] == "i002")
    assert explore["kind"] == "explore"
    assert explore["status"] == "pending"
    assert explore["todo_id"] == "td001"
    assert explore["execute_timeout_seconds"] == 420
    assert explore["conclude_timeout_seconds"] == 180
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "explore"
    evaluation = client.get(f"/api/v1/tasks/{task['id']}/goal-evaluations").json()[0]
    assert evaluation["evidence_refs"] == ["todo:td001"]
    reason_board = client.get(f"/api/v1/tasks/{task['id']}/diagnostics").json()[
        "worker_rankings"
    ]["boards"]["reason"]
    assert reason_board["total"] == 1
    assert reason_board["success"] == 1

    repeated = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": reason_claim["lease_id"]},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["created_intent_ids"] == ["i002"]
    audit_path = (
        data_dir
        / "runs"
        / task["id"]
        / reason_claim["reason_run_id"]
        / reason_claim["lease_id"]
        / "reason_plan.json"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["created_todo_ids"] == ["td001"]
    assert audit["created_intent_ids"] == ["i002"]
    assert "vulnerability_reports" not in audit["draft"]


def test_reason_rejects_unknown_goal_todo_ref_and_rolls_back(client) -> None:
    task, reason_claim, completed = active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    draft = {
        "todo_adds": [
            {
                "ref": "authz",
                "title": "验证对象级授权",
                "category": "authorization",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "intent_proposals": [
            {
                "todo_ref": "authz",
                "from_facts": [completed["fact_id"]],
                "description": "验证订单详情接口是否存在对象级授权问题",
                "targets": ["a.example.com"],
                "execute_seconds": 420,
                "conclude_seconds": 180,
            }
        ],
        "goal_assessment": {
            "achieved": False,
            "reason": "仍需继续测试",
            "evidence_refs": ["todo:missing"],
        },
    }

    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        draft,
    )

    assert response.status_code == 409
    assert "Goal evidence not found: todo:missing" in response.text
    assert client.get(f"/api/v1/tasks/{task['id']}/todos").json() == []
    assert client.get(f"/api/v1/tasks/{task['id']}/goal-evaluations").json() == []
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert [item["id"] for item in graph["intents"]] == ["i001"]


def test_reason_workspace_open_is_idempotent_for_the_same_reason_lease(client) -> None:
    task, reason_claim, _completed = active_reason(client)
    payload = {
        "reason_run_id": reason_claim["reason_run_id"],
        "lease_id": reason_claim["lease_id"],
        "context_revision": reason_claim["context_revision"],
    }

    first = client.post(f"/api/v1/tasks/{task['id']}/reason/workspaces", json=payload)
    second = client.post(f"/api/v1/tasks/{task['id']}/reason/workspaces", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_reason_workspace_mutations_require_the_current_active_lease(client) -> None:
    task, reason_claim, completed = active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    assert "vulnerability_reports" not in workspace["draft"]
    todo = {
        "ref": "lease-test",
        "title": "验证租约边界",
        "category": "other",
        "sources": [{"type": "fact", "id": completed["fact_id"]}],
    }

    stale = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/todos",
        json={"lease_id": "lease-stale", "todo": todo},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "lease_inactive"

    stopped = client.post(f"/api/v1/tasks/{task['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    after_stop = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/todos",
        json={"lease_id": reason_claim["lease_id"], "todo": todo},
    )
    assert after_stop.status_code == 409
    assert after_stop.json()["error"]["code"] == "lease_inactive"


def test_reason_workspace_has_no_whole_draft_submit_endpoint(client) -> None:
    task, reason_claim, _completed = active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()

    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/submit",
        json={"lease_id": reason_claim["lease_id"], "draft": {}},
    )

    assert response.status_code in {404, 405}


def test_reason_todo_can_reference_enabled_project_knowledge(client) -> None:
    task, reason_claim, _completed = active_reason(client)
    knowledge = client.post(
        f"/api/v1/projects/{task['project_id']}/knowledge",
        json={
            "title": "历史鉴权约束",
            "content": "该系统的管理接口统一使用 X-Admin-Token。",
        },
    ).json()
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()

    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/todos",
        json={
            "lease_id": reason_claim["lease_id"],
            "todo": {
                "ref": "knowledge-auth",
                "title": "依据项目知识验证管理接口鉴权",
                "category": "authentication",
                "sources": [{"type": "knowledge", "id": knowledge["id"]}],
            },
        },
    )

    assert response.status_code == 200, response.text


def test_reason_can_complete_a_new_todo_by_its_draft_ref(client) -> None:
    task, reason_claim, completed = active_reason(client)
    finding_artifact_id = client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()[2]["id"]
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    draft = {
        "todo_adds": [
            {
                "ref": "confirmed-authz",
                "title": "记录已确认的未授权访问",
                "category": "authorization",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "todo_updates": [
            {
                "todo_id": "confirmed-authz",
                "status": "completed",
                "status_reason": "Bootstrap 已提供充分证据",
                "source_adds": [{"type": "artifact", "id": finding_artifact_id}],
            }
        ],
        "intent_proposals": [],
        "goal_assessment": {
            "achieved": True,
            "reason": "既定验证目标已经有充分证据",
            "evidence_refs": [f"fact:{completed['fact_id']}"],
        },
    }

    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        draft,
    )

    assert response.status_code == 200, response.text
    todo = client.get(f"/api/v1/tasks/{task['id']}/todos").json()[0]
    assert todo["status"] == "completed"
    assert todo["status_reason"] == "Bootstrap 已提供充分证据"
    assert todo["completed_at"] is not None
    assert todo["sources"] == [
        {"type": "artifact", "id": finding_artifact_id},
        {"type": "fact", "id": completed["fact_id"]},
    ]
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "completed"


def test_reason_scope_failure_rolls_back_the_whole_plan(client) -> None:
    task, reason_claim, completed = active_reason(client)
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    draft = {
        "todo_adds": [
            {
                "ref": "outside",
                "title": "测试其他系统",
                "category": "other",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "todo_updates": [],
        "intent_proposals": [
            {
                "todo_ref": "outside",
                "from_facts": [completed["fact_id"]],
                "description": "测试其他系统",
                "targets": ["b.example.com"],
                "execute_seconds": 300,
                "conclude_seconds": 180,
            }
        ],
    }

    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        draft,
    )

    assert response.status_code == 409
    assert client.get(f"/api/v1/tasks/{task['id']}/todos").json() == []
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert [item["id"] for item in graph["intents"]] == ["i001"]


def test_reason_rejects_goal_as_an_explore_intent_source(client) -> None:
    task, reason_claim, completed = active_reason(client)
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
                        "ref": "invalid-goal-source",
                        "title": "错误来源测试",
                        "category": "other",
                        "sources": [{"type": "fact", "id": completed["fact_id"]}],
                    }
                ],
                "intent_proposals": [
                    {
                        "todo_ref": "invalid-goal-source",
                        "from_facts": ["goal"],
                        "description": "不应从 Goal 派发 Explore Intent",
                        "targets": ["a.example.com"],
                        "execute_seconds": 300,
                        "conclude_seconds": 180,
                    }
                ],
        },
    )

    assert response.status_code == 409
    assert "Goal Fact cannot be used as an Intent source" in response.json()["error"]["message"]
    assert client.get(f"/api/v1/tasks/{task['id']}/todos").json() == []


def test_reason_scope_accepts_the_explicit_origin_port(client) -> None:
    task, reason_claim, completed = active_reason(
        client,
        origin="http://127.0.0.1:18081",
    )
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    draft = {
        "todo_adds": [
            {
                "ref": "verify-origin",
                "title": "复核起点服务",
                "category": "recon",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "intent_proposals": [
            {
                "todo_ref": "verify-origin",
                "from_facts": [completed["fact_id"]],
                "description": "复核起点端口上的服务",
                "targets": ["http://127.0.0.1:18081"],
                "execute_seconds": 300,
                "conclude_seconds": 180,
            }
        ],
    }

    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        draft,
    )

    assert response.status_code == 200, response.text
    explore = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][-1]
    assert explore["targets"] == ["http://127.0.0.1:18081"]


def test_reason_scope_rejects_a_different_port_on_the_origin_host(client) -> None:
    task, reason_claim, completed = active_reason(
        client,
        origin="http://127.0.0.1:18081",
    )
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_claim["reason_run_id"],
            "lease_id": reason_claim["lease_id"],
            "context_revision": reason_claim["context_revision"],
        },
    ).json()
    draft = {
        "todo_adds": [
            {
                "ref": "wrong-port",
                "title": "错误端口",
                "category": "recon",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "intent_proposals": [
            {
                "todo_ref": "wrong-port",
                "from_facts": [completed["fact_id"]],
                "description": "访问未授权端口",
                "targets": ["127.0.0.1:18080"],
                "execute_seconds": 300,
                "conclude_seconds": 180,
            }
        ],
    }

    response = apply_reason_draft(
        client,
        workspace["id"],
        reason_claim["lease_id"],
        draft,
    )

    assert response.status_code == 409
    assert client.get(f"/api/v1/tasks/{task['id']}/todos").json() == []
