from __future__ import annotations

from sqlalchemy import update

from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from protocol_helpers import (
    apply_reason_draft,
    bootstrap_files_from_legacy,
    claim_reason_run,
    complete_bootstrap,
    open_reason_workspace,
)

BOOTSTRAP_PAYLOAD = {
    "summary": "完成初始资产发现。",
    "artifacts": {
        "asset_inventory": "# 资产\n\n- a.example.com",
        "initial_leads": "# 线索\n\n- 复用普通成员会话验证权限链",
        "findings": "# 已确认问题\n\n- 本轮无确认漏洞",
    },
}

OBJECTIVE = {
    "initial_principal": {"id": "user:member", "label": "普通成员", "tenant": "tenant-a"},
    "target_capabilities": ["tenant:admin"],
    "target_assets": ["member-management"],
    "data_targets": [
        {
            "id": "customer-records",
            "max_records": 5,
            "max_bytes": 4096,
            "allowed_fields": ["id", "status"],
        }
    ],
}


def _create_task(client, *, red_team: bool) -> dict:
    project = client.post(
        "/api/v1/projects", json={"title": "红队" if red_team else "漏洞"}
    ).json()
    payload = {
        "project_id": project["id"],
        "title": "任务",
        "origin": "a.example.com",
        "goal": "取得目标能力并读取限定数据" if red_team else "挖掘高置信度漏洞",
        "worker_pool": ["worker-a"],
    }
    if red_team:
        payload["engagement_mode"] = "red_team"
        payload["red_team_objective"] = OBJECTIVE
    response = client.post("/api/v1/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _active_reason(client, *, red_team: bool) -> tuple[dict, dict, dict]:
    task = _create_task(client, red_team=red_team)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 1},
    )
    bootstrap_claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    )
    assert bootstrap_claim.status_code == 200, bootstrap_claim.text
    bootstrap_lease = bootstrap_claim.json()["lease_id"]
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": bootstrap_lease}).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_lease,
        files=bootstrap_files_from_legacy(BOOTSTRAP_PAYLOAD),
    )
    reason_claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
        worker_name="worker-a",
    )
    return task, reason_claim, completed


def _chain_draft(completed: dict, *, link: bool = True) -> dict:
    draft = {
        "todo_adds": [
            {
                "ref": "chain-todo",
                "title": "打通到租户管理员",
                "status": "open",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
        ],
        "intent_proposals": [
            {
                "ref": "chain-intent",
                "todo_ref": "chain-todo",
                "from_facts": [completed["fact_id"]],
                "description": "复用普通成员会话验证成员管理接口的权限链",
                "targets": ["a.example.com"],
            }
        ],
        "chain_adds": [
            {
                "ref": "ch-1",
                "title": "从普通成员到租户管理员",
                "target_capabilities": ["tenant:admin"],
                "target_assets": ["member-management"],
                "source_fact_ids": [completed["fact_id"]],
            }
        ],
    }
    if link:
        draft["chain_intent_links"] = [
            {"chain_ref": "ch-1", "intent_ref": "chain-intent"}
        ]
    return draft


def _claim_explore(client, task_id: str, intent_id: str) -> tuple[dict, dict]:
    base = f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
    assert claim.status_code == 200, claim.text
    lease = claim.json()["lease_id"]
    assert client.post(f"{base}/start", json={"lease_id": lease}).status_code == 200
    execution = client.post(f"{base}/input", json={"lease_id": lease})
    assert execution.status_code == 200, execution.text
    return claim.json(), execution.json()


def test_chain_staging_requires_red_team_task(client) -> None:
    """A vulnerability-mode Workspace must reject RedTeam Chain staging."""

    task, claim, completed = _active_reason(client, red_team=False)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    lease = {"lease_id": claim["lease_id"]}
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/chains",
        json={
            **lease,
            "chain": {
                "ref": "ch-1",
                "title": "不该被接受的链",
                "target_capabilities": [],
                "target_assets": [],
                "source_fact_ids": [],
            },
        },
    )
    assert response.status_code == 409, response.text
    assert "red_team tasks" in response.text


def test_chain_target_must_stay_within_objective(client) -> None:
    """A staged Chain may not exceed the Objective capability set."""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    lease = {"lease_id": claim["lease_id"]}
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/chains",
        json={
            **lease,
            "chain": {
                "ref": "ch-1",
                "title": "越界链",
                "target_capabilities": ["tenant:superadmin"],
                "target_assets": ["member-management"],
                "source_fact_ids": [completed["fact_id"]],
            },
        },
    )
    assert response.status_code == 409, response.text
    assert "exceeds the task objective" in response.text


def test_chain_update_rejects_control_plane_status(client) -> None:
    """Workers may only move a Chain to planned/blocked/abandoned."""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    lease = {"lease_id": claim["lease_id"]}
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/chain-updates",
        json={**lease, "update": {"chain_id": "ch000001", "status": "achieved"}},
    )
    assert response.status_code == 422, response.text


def test_chain_link_requires_known_refs(client) -> None:
    """A staged link must reference an in-draft Chain ref and a proposed Intent ref."""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    lease = {"lease_id": claim["lease_id"]}
    draft = _chain_draft(completed)
    draft.pop("chain_intent_links")

    # Stage the Todo, the chain and the intent, then link to unknown refs.
    for todo in draft["todo_adds"]:
        response = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/todos",
            json={**lease, "todo": todo},
        )
        assert response.status_code == 200, response.text
    for chain in draft["chain_adds"]:
        response = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/chains",
            json={**lease, "chain": chain},
        )
        assert response.status_code == 200, response.text
    for intent in draft["intent_proposals"]:
        response = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/intents",
            json={**lease, "intent": intent},
        )
        assert response.status_code == 200, response.text

    missing_chain = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/chain-links",
        json={**lease, "link": {"chain_ref": "ch-missing", "intent_ref": "chain-intent"}},
    )
    assert missing_chain.status_code == 404, missing_chain.text
    missing_intent = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/chain-links",
        json={**lease, "link": {"chain_ref": "ch-1", "intent_ref": "i-missing"}},
    )
    assert missing_intent.status_code == 404, missing_intent.text


def test_reason_commit_creates_chain_and_binds_intent(client) -> None:
    """Commit persists the staged Chain, binds the Intent and reaches the Worker."""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    assert committed.status_code == 200, committed.text
    result = committed.json()
    assert len(result["created_chain_ids"]) == 1
    chain_id = result["created_chain_ids"][0]
    assert chain_id.startswith("ch")
    intent_id = result["created_intent_ids"][0]

    chains = client.get(f"/api/v1/tasks/{task['id']}/red-team/chains")
    assert chains.status_code == 200, chains.text
    [chain] = chains.json()
    assert chain["id"] == chain_id
    assert chain["status"] == "planned"
    # 初始身份默认取 Objective 的初始主体
    assert chain["initial_principal"]["id"] == "user:member"
    assert chain["source_intent_ids"] == [intent_id]
    assert chain["created_by"].startswith("reason")

    # 绑定的 Chain 通过 Explore 执行输入下发（AXIOM_CHAIN_ID 的来源）
    _claim, execution = _claim_explore(client, task["id"], intent_id)
    assert execution["chain_id"] == chain_id
    assert execution["engagement_mode"] == "red_team"


def test_result_report_enforces_bound_chain(client) -> None:
    """A Result must be reported to the Intent's bound Chain and creates no Vulnerability."""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    draft = _chain_draft(completed)
    draft["chain_adds"].append(
        {
            "ref": "ch-2",
            "title": "未被绑定的链",
            "target_capabilities": [],
            "target_assets": [],
            "source_fact_ids": [],
        }
    )
    committed = apply_reason_draft(client, workspace["id"], claim["lease_id"], draft)
    assert committed.status_code == 200, committed.text
    result = committed.json()
    chain_id, other_chain_id = result["created_chain_ids"]
    intent_id = result["created_intent_ids"][0]

    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    lease = {"lease_id": claim_body["lease_id"]}
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"permission transition proof")},
        data={"actor": "worker-a", "summary": "权限跃迁证据"},
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact_id = uploaded.json()["id"]
    report = {
        "chain_id": other_chain_id,
        "result_type": "permission",
        "title": "取得成员管理能力",
        "description": "复用普通成员会话调用成员管理接口成功",
        "principal": "user:member",
        "capabilities_gained": ["tenant:admin"],
        "target_asset": "member-management",
        "evidence_refs": [f"artifact:{artifact_id}"],
    }

    misplaced = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={**lease, "report": report},
    )
    assert misplaced.status_code == 409, misplaced.text
    assert "bound Chain" in misplaced.text

    report["chain_id"] = chain_id
    accepted = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={**lease, "report": report},
    )
    assert accepted.status_code == 201, accepted.text
    created = accepted.json()
    assert created["status"] == "reviewing"
    assert created["chain_id"] == chain_id
    # 红队成果不写入漏洞记录
    vulnerabilities = client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities")
    assert vulnerabilities.status_code == 200, vulnerabilities.text
    assert vulnerabilities.json() == []

    results = client.get(
        f"/api/v1/tasks/{task['id']}/red-team/results", params={"chain_id": chain_id}
    )
    assert results.status_code == 200, results.text
    assert [item["id"] for item in results.json()] == [created["id"]]


def test_vulnerability_task_rejects_redteam_result(client) -> None:
    """RedTeam endpoints stay closed for vulnerability-mode tasks."""

    task, claim, completed = _active_reason(client, red_team=False)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/intents/i001/red-team-results",
        json={
            "lease_id": "lease-x",
            "report": {
                "chain_id": "ch000001",
                "result_type": "permission",
                "title": "越界成果",
                "description": "漏洞任务不得上报红队成果",
                "principal": "user:member",
                "evidence_refs": ["artifact:x"],
            },
        },
    )
    assert response.status_code in (404, 409), response.text


def test_plan_workspace_commits_chain_and_binds_intent(client, engine) -> None:
    """The independent Plan runtime stages and commits Chains through the same path."""

    project = client.post("/api/v1/projects", json={"title": "红队 Plan"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Plan 红队",
            "origin": "a.example.com",
            "goal": "取得目标能力并读取限定数据",
            "worker_pool": ["plan-worker"],
            "engagement_mode": "red_team",
            "red_team_objective": OBJECTIVE,
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/plan-worker",
        json={"capabilities": ["bootstrap", "plan", "explore"], "max_running": 1},
    )
    assert worker.status_code == 200, worker.text
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "plan-worker"},
    )
    assert claim.status_code == 200, claim.text
    bootstrap_lease = claim.json()["lease_id"]
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": bootstrap_lease}).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_lease,
        files=bootstrap_files_from_legacy(BOOTSTRAP_PAYLOAD),
    )
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.id == completed["next_reason_run_id"],
            )
            .values(status="completed")
        )
        conn.execute(
            update(tasks).where(tasks.c.id == task["id"]).values(phase="waiting")
        )

    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()

    plan_base = f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{run['id']}"
    plan_claim = client.post(f"{plan_base}/claim", json={"worker_name": "plan-worker"})
    assert plan_claim.status_code == 200, plan_claim.text
    plan_lease = plan_claim.json()["lease_id"]
    assert client.post(f"{plan_base}/start", json={"lease_id": plan_lease}).status_code == 200
    execution = client.post(f"{plan_base}/input", json={"lease_id": plan_lease})
    assert execution.status_code == 200, execution.text

    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": plan_lease,
            "context_revision": execution.json()["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    workspace_id = workspace.json()["id"]
    lease = {"lease_id": plan_lease}

    todo = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/todos",
        json={
            **lease,
            "todo": {
                "ref": "chain-todo",
                "title": "打通权限链",
                "category": "authorization",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            },
        },
    )
    assert todo.status_code == 200, todo.text
    intent = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/intents",
        json={
            **lease,
            "intent": {
                "ref": "chain-intent",
                "todo_ref": "chain-todo",
                "from_facts": [completed["fact_id"]],
                "source_refs": [f"fact:{completed['fact_id']}"],
                "description": "验证成员管理接口的权限链",
                "targets": ["a.example.com"],
                "endpoint": "/api/v1/members",
                "test_vector": "role escalation",
                "why_now": "Plan 复盘发现能力覆盖缺口",
                "expected_signal": "取得成员管理能力",
                "stop_condition": "达成权限跃迁后停止",
            },
        },
    )
    assert intent.status_code == 200, intent.text
    chain = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/chains",
        json={
            **lease,
            "chain": {
                "ref": "ch-1",
                "title": "从普通成员到租户管理员",
                "target_capabilities": ["tenant:admin"],
                "target_assets": ["member-management"],
                "source_fact_ids": [completed["fact_id"]],
            },
        },
    )
    assert chain.status_code == 200, chain.text
    link = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/chain-links",
        json={**lease, "link": {"chain_ref": "ch-1", "intent_ref": "chain-intent"}},
    )
    assert link.status_code == 200, link.text
    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/decision",
        json={
            **lease,
            "decision": {
                "decision": "dispatch_explore",
                "reviewed_dimensions": ["coverage"],
                "confidence": "medium",
            },
        },
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(f"/api/v1/plan-workspaces/{workspace_id}/commit", json=lease)
    assert committed.status_code == 200, committed.text
    payload = committed.json()
    assert len(payload["created_chain_ids"]) == 1
    chain_id = payload["created_chain_ids"][0]
    assert chain_id.startswith("ch")
    intent_id = payload["created_intent_ids"][0]

    chains = client.get(f"/api/v1/tasks/{task['id']}/red-team/chains")
    assert chains.status_code == 200, chains.text
    [chain_record] = chains.json()
    assert chain_record["id"] == chain_id
    assert chain_record["created_by"].startswith("plan")
    assert chain_record["source_intent_ids"] == [intent_id]

    # 幂等重提交返回相同的 Chain 结果
    recommitted = client.post(f"/api/v1/plan-workspaces/{workspace_id}/commit", json=lease)
    assert recommitted.status_code == 200, recommitted.text
    assert recommitted.json()["created_chain_ids"] == [chain_id]
