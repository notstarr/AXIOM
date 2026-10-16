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


def _chain_draft(completed: dict) -> dict:
    return {
        "chain_adds": [
            {
                "ref": "ch-1",
                "title": "从普通成员到租户管理员",
                "target_capabilities": ["tenant:admin"],
                "target_assets": ["member-management"],
                "source_fact_ids": [completed["fact_id"]],
            }
        ],
        "intent_proposals": [
            {
                "ref": "chain-intent",
                "chain_ref": "ch-1",
                "from_facts": [completed["fact_id"]],
                "description": "复用普通成员会话验证成员管理接口的权限链",
                "targets": ["a.example.com"],
            }
        ],
    }


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


def test_result_review_points_rules(client) -> None:
    """核定规则：verified 必须带分；duplicate 强制 0 分。"""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    assert committed.status_code == 200, committed.text
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]

    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"permission transition proof")},
        data={"actor": "worker-a", "summary": "权限跃迁证据"},
    )
    report = {
        "chain_id": chain_id,
        "result_type": "permission",
        "title": "取得成员管理能力",
        "description": "复用普通成员会话调用成员管理接口成功",
        "principal": "user:member",
        "capabilities_gained": ["tenant:admin"],
        "target_asset": "member-management",
        "evidence_refs": [f"artifact:{uploaded.json()['id']}"],
        "points_declared": 40,
    }
    accepted = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={"lease_id": claim_body["lease_id"], "report": report},
    )
    assert accepted.status_code == 201, accepted.text
    result_id = accepted.json()["id"]

    # verified 不带核定分 → 拒绝
    no_points = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/{result_id}/review",
        json={"status": "verified", "reason": "证据充分，符合垂直提权档位", "reviewed_by": "reviewer-a"},
    )
    assert no_points.status_code == 409, no_points.text

    # duplicate 即使给了分也强制 0
    duplicated = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/results/{result_id}/review",
        json={"status": "duplicate", "reason": "与既有成果为同一能力，重复计分", "reviewed_by": "reviewer-a", "points_awarded": 30},
    )
    assert duplicated.status_code == 200, duplicated.text
    assert duplicated.json()["status"] == "duplicate"
    assert duplicated.json()["points_awarded"] == 0


def test_redteam_goal_requires_target_points(client, engine) -> None:
    """Goal 达成判定：verified 核定分之和必须达到 target_points。"""

    import pytest as _pytest
    from sqlalchemy import insert, update

    from axiom.modules.goals.service import GoalService
    from axiom.modules.redteam.tables import red_team_chains, red_team_results
    from axiom.shared.errors import ConflictError

    project = client.post("/api/v1/projects", json={"title": "积分门槛"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "积分门槛任务",
            "origin": "a.example.com",
            "goal": "对该系统进行渗透测试，并取得100积分",
            "worker_pool": ["worker-a"],
            "engagement_mode": "red_team",
        },
    ).json()

    service = GoalService(engine)
    with engine.connect() as conn:
        loaded = service.tasks.get(conn, task["id"])
        assert loaded is not None and loaded.red_team_objective is not None
        assert loaded.red_team_objective.target_points == 100
        now = "2026-10-14T00:00:00Z"
        conn.execute(
            insert(red_team_chains).values(
                id="chGOAL01", task_id=task["id"], title="链",
                initial_principal_json={"id": "user:initial", "label": "初始身份", "tenant": "", "credential_id": None},
                target_capabilities_json=[], target_assets_json=[], current_state_json={},
                source_fact_ids_json=[], source_intent_ids_json=[],
                status="open", created_by="worker.reason", created_at=now, updated_at=now,
            )
        )
        base_result = dict(
            id="rrGOAL01", task_id=task["id"], chain_id="chGOAL01", intent_id="i001",
            result_type="permission", title="成果", description="desc", principal="user:initial", tenant="",
            capabilities_before_json=[], capabilities_gained_json=["tenant:admin"], capabilities_after_json=["tenant:admin"],
            target_asset="member-management", data_target_id=None, sample_count=None, sample_bytes=None,
            data_fields_json=[], data_summary="", credential_ref=None, evidence_refs_json=[],
            status="verified", reported_by="worker-a", reviewed_by="reviewer-a", review_reason="ok",
            points_declared=30, points_awarded=30, created_at=now, updated_at=now,
        )
        conn.execute(insert(red_team_results).values(**base_result))

        # 30 分 < 目标 100 → 拒绝
        with _pytest.raises(ConflictError, match="verified points"):
            service._validate_red_team_completion(conn, task["id"], loaded.red_team_objective)

        # 补到 100 分 → 通过
        conn.execute(
            update(red_team_results)
            .where(red_team_results.c.id == "rrGOAL01")
            .values(points_awarded=100)
        )
        service._validate_red_team_completion(conn, task["id"], loaded.red_team_objective)


def test_red_team_default_objective_is_permissive(client) -> None:
    """只切模式不填结构化目标：后端合成宽松默认，链可自由声明能力/资产。"""

    project = client.post("/api/v1/projects", json={"title": "默认目标"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "只切模式",
            "origin": "a.example.com",
            "goal": "取得目标能力并读取限定数据",
            "worker_pool": ["worker-a"],
            "engagement_mode": "red_team",
        },
    )
    assert task.status_code == 201, task.text
    task = task.json()

    detail = client.get(f"/api/v1/tasks/{task['id']}")
    assert detail.status_code == 200, detail.text
    objective = detail.json().get("red_team_objective")
    assert objective, "红队任务应合成默认目标"
    assert objective["initial_principal"]["id"] == "user:initial"
    assert objective["target_capabilities"] == []

    # 未声明能力/资产上限 → 链可自由声明任意目标
    chain = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/chains",
        params={"created_by": "worker.reason"},
        json={
            "title": "自由声明的链",
            "initial_principal": {"id": "user:initial", "label": "初始身份"},
            "target_capabilities": ["anything:admin"],
            "target_assets": ["any-asset"],
            "source_fact_ids": [],
        },
    )
    assert chain.status_code == 201, chain.text


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
    assert chain["status"] == "open"
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
        "points_declared": 30,
    }

    misplaced = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={**lease, "report": report},
    )
    assert misplaced.status_code == 409, misplaced.text
    assert "bound Chain" in misplaced.text

    omitted = {key: value for key, value in report.items() if key != "chain_id"}
    accepted = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={**lease, "report": omitted},
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


def test_redteam_result_review_lifecycle(client, engine) -> None:
    """成果全链路：上报 → 审核入队 → Reviewer 领取 → 裁决 → 状态映射。"""

    from sqlalchemy import select

    from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    assert committed.status_code == 200, committed.text
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]

    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"permission transition proof")},
        data={"actor": "worker-a", "summary": "权限跃迁证据"},
    )
    assert uploaded.status_code == 201, uploaded.text
    report = {
        "chain_id": chain_id,
        "result_type": "permission",
        "title": "取得成员管理能力",
        "description": "复用普通成员会话调用成员管理接口成功",
        "principal": "user:member",
        "capabilities_gained": ["tenant:admin"],
        "target_asset": "member-management",
        "evidence_refs": [f"artifact:{uploaded.json()['id']}"],
        "points_declared": 30,
    }
    accepted = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={"lease_id": claim_body["lease_id"], "report": report},
    )
    assert accepted.status_code == 201, accepted.text
    result_id = accepted.json()["id"]
    # 上报即进入审核（enqueue_redteam_in）
    assert accepted.json()["status"] == "reviewing"

    with engine.connect() as conn:
        review_run_id = conn.execute(
            select(vulnerability_review_runs.c.id).where(
                vulnerability_review_runs.c.task_id == task["id"],
                vulnerability_review_runs.c.red_team_result_id == result_id,
            )
        ).scalar_one()

    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore", "review"], "max_running": 1},
    )
    # 释放 Explore 租约，腾出 Worker 容量供 Reviewer 领取
    released = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}/release",
        json={"lease_id": claim_body["lease_id"], "failure_kind": "test_release"},
    )
    assert released.status_code == 200, released.text
    review_base = f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{review_run_id}"
    claimed = client.post(f"{review_base}/claim", json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text
    review_lease = {"lease_id": claimed.json()["lease_id"]}

    started = client.post(f"{review_base}/start", json=review_lease)
    assert started.status_code == 200, started.text
    decided = client.post(
        f"{review_base}/decision",
        json={**review_lease, "decision": "verified", "reason": "证据充分：权限跃迁可复现，链归属与目标一致", "points_awarded": 30},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "verified"
    assert decided.json()["review_subject_type"] == "redteam_result"

    concluded = client.post(f"{review_base}/conclude", json=review_lease)
    assert concluded.status_code == 204, concluded.text
    completed_review = client.post(f"{review_base}/complete", json=review_lease)
    assert completed_review.status_code == 200, completed_review.text
    body = completed_review.json()
    assert body["review_subject_type"] == "redteam_result"
    assert body["red_team_result_id"] == result_id
    assert body["vulnerability_status"] == "verified"
    assert body["points_awarded"] == 30

    results = client.get(
        f"/api/v1/tasks/{task['id']}/red-team/results", params={"chain_id": chain_id}
    )
    assert results.status_code == 200, results.text
    [verified] = results.json()
    assert verified["id"] == result_id
    assert verified["status"] == "verified"
    assert verified["points_declared"] == 30
    assert verified["points_awarded"] == 30
    assert verified["reviewed_by"]


def test_cross_task_result_and_chain_listing(client) -> None:
    """重要事实·成果视图的全局列表（分页 + 筛选 + X-Total-Count）。"""

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    assert committed.status_code == 200, committed.text
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]

    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"permission transition proof")},
        data={"actor": "worker-a", "summary": "权限跃迁证据"},
    )
    assert uploaded.status_code == 201, uploaded.text
    report = {
        "chain_id": chain_id,
        "result_type": "permission",
        "title": "取得成员管理能力",
        "description": "复用普通成员会话调用成员管理接口成功",
        "principal": "user:member",
        "capabilities_gained": ["tenant:admin"],
        "target_asset": "member-management",
        "evidence_refs": [f"artifact:{uploaded.json()['id']}"],
        "points_declared": 30,
    }
    accepted = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={"lease_id": claim_body["lease_id"], "report": report},
    )
    assert accepted.status_code == 201, accepted.text

    results = client.get("/api/v1/red-team/results")
    assert results.status_code == 200, results.text
    assert results.headers["X-Total-Count"] == "1"
    [item] = results.json()
    assert item["chain_id"] == chain_id

    filtered = client.get(
        "/api/v1/red-team/results",
        params={"task_id": task["id"], "status": "reviewing", "type": "permission"},
    )
    assert filtered.headers["X-Total-Count"] == "1"
    by_search = client.get("/api/v1/red-team/results", params={"search": "成员管理"})
    assert by_search.headers["X-Total-Count"] == "1"
    by_status = client.get("/api/v1/red-team/results", params={"status": "verified"})
    assert by_status.headers["X-Total-Count"] == "0"

    chains = client.get("/api/v1/red-team/chains")
    assert chains.status_code == 200, chains.text
    assert chains.headers["X-Total-Count"] == "1"
    assert chains.json()[0]["id"] == chain_id


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
                "points_declared": 10,
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
    intent = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/intents",
        json={
            **lease,
            "intent": {
                "ref": "chain-intent",
                "chain_ref": "ch-1",
                "from_facts": [completed["fact_id"]],
                "source_refs": [f"fact:{completed['fact_id']}"],
                "description": "验证成员管理接口的权限链",
                "targets": ["a.example.com"],
                "endpoint": "/api/v1/members",
                "test_vector": "role escalation",
                "why_now": "Plan 复盘发现这条链的下一步",
                "expected_signal": "取得成员管理能力",
                "stop_condition": "达成权限跃迁后停止",
            },
        },
    )
    assert intent.status_code == 200, intent.text
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


def test_redteam_intent_requires_chain_and_rejects_todo(client) -> None:
    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    lease = {"lease_id": claim["lease_id"]}
    missing = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intents",
        json={
            **lease,
            "intent": {
                "from_facts": [completed["fact_id"]],
                "description": "没有父节点的红队步骤",
                "targets": ["a.example.com"],
            },
        },
    )
    assert missing.status_code == 409, missing.text
    assert "Chain" in missing.text


def test_vulnerability_intent_rejects_chain(client) -> None:
    task, claim, completed = _active_reason(client, red_team=False)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    lease = {"lease_id": claim["lease_id"]}
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intents",
        json={
            **lease,
            "intent": {
                "chain_ref": "ch-1",
                "from_facts": [completed["fact_id"]],
                "description": "漏洞模式不能挂链",
                "targets": ["a.example.com"],
            },
        },
    )
    assert response.status_code == 409, response.text
    assert "Todo" in response.text


def test_completed_chain_rejects_new_intent(client, engine) -> None:
    from axiom.modules.redteam.tables import red_team_chains

    task, claim, completed = _active_reason(client, red_team=True)
    created = client.post(
        f"/api/v1/tasks/{task['id']}/red-team/chains",
        params={"created_by": "worker.reason"},
        json={
            "title": "已走完的链",
            "initial_principal": {"id": "user:member", "label": "普通成员"},
            "target_capabilities": ["tenant:admin"],
            "target_assets": ["member-management"],
            "source_fact_ids": [],
        },
    )
    assert created.status_code == 201, created.text
    chain_id = created.json()["id"]
    with engine.begin() as conn:
        conn.execute(
            update(red_team_chains)
            .where(red_team_chains.c.task_id == task["id"], red_team_chains.c.id == chain_id)
            .values(status="completed", status_reason="这条路径不再派发")
        )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intents",
        json={
            "lease_id": claim["lease_id"],
            "intent": {
                "chain_id": chain_id,
                "from_facts": [completed["fact_id"]],
                "description": "终态链上的新步骤",
                "targets": ["a.example.com"],
            },
        },
    )
    assert response.status_code == 409, response.text
    assert chain_id in response.text


def test_open_chain_without_inflight_intent_wakes_reason(client, engine) -> None:
    from axiom.modules.graph.tables import intents
    from protocol_helpers import update_runtime_settings

    update_runtime_settings(client, reason_min_interval_seconds=0)
    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    assert committed.status_code == 200, committed.text
    intent_id = committed.json()["created_intent_ids"][0]
    now = "2026-10-15T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == intent_id)
            .values(status="completed", assessment_status="completed", concluded_at=now)
        )
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"])
            .values(status="completed", concluded_at=now, updated_at=now)
        )
    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    created = reconciled.json()["created_reason_run_ids"]
    assert created, reconciled.text
    assert completed["next_reason_run_id"] not in created


def test_redteam_review_closeout_does_not_require_vuln_report(tmp_path) -> None:
    from axiom.dispatcher.review import ReviewRunner
    from axiom.modules.execution.schemas import ExecutionInput
    from axiom.modules.vulnerability_reviews.schemas import (
        ClaimedVulnerabilityReviewRun,
        ReviewStatus,
    )

    class Plane:
        def review_status(self, work):
            return ReviewStatus(
                review_run_id=work.review_run_id,
                task_id=work.task_id,
                review_subject_type="redteam_result",
                red_team_result_id=work.red_team_result_id,
                status="running",
                decision="verified",
                ready=True,
            )

    runner = ReviewRunner(Plane())
    work = ClaimedVulnerabilityReviewRun(
        task_id="t_001",
        review_run_id="vr1",
        review_subject_type="redteam_result",
        red_team_result_id="rr1",
        worker_name="worker-a",
        lease_id="lease",
        claimed_at="2026-10-15T00:00:00Z",
    )
    execution_input = ExecutionInput(
        task_id="t_001",
        engagement_mode="red_team",
        review_run_id="vr1",
        red_team_result_id="rr1",
        lease_id="lease",
        worker_name="worker-a",
        intent_kind="review",
        origin="a.example.com",
        goal="取得100积分",
        scope={},
        run_dir=str(tmp_path),
    )
    runner._validate_output(work, execution_input, "FINISH")


def test_exercise_rubric_accepts_target_control_score() -> None:
    """护网表单项最高 60，总分不封顶。旧的 50 分上限不能挡住靶标完全控制。"""

    from pathlib import Path

    import pytest
    from pydantic import ValidationError

    from axiom.modules.redteam.schemas import MAX_RESULT_POINTS, RedTeamResultReport, RedTeamResultReview

    report = RedTeamResultReport(
        result_type="permission",
        title="靶标系统完全控制",
        description="可任意操作目标系统",
        principal="user:member",
        target_asset="target-system",
        evidence_refs=["artifact:a001"],
        points_declared=60,
    )
    assert report.points_declared == 60
    review = RedTeamResultReview(
        status="verified",
        reason="靶标完全控制，证据可复现",
        reviewed_by="reviewer-a",
        points_awarded=60,
    )
    assert review.points_awarded == 60
    with pytest.raises(ValidationError):
        RedTeamResultReport(
            result_type="permission",
            title="超出字段宽度",
            description="技术上限与目标分一致",
            principal="user:member",
            evidence_refs=["artifact:a001"],
            points_declared=MAX_RESULT_POINTS + 1,
        )

    scoring = Path(__file__).resolve().parents[1] / "src/axiom/dispatcher/skills/redteam-scoring/SKILL.md"
    text = scoring.read_text(encoding="utf-8")
    assert "靶标系统 · 完全控制" in text
    assert "每个 | 60" in text
    assert "不设上限" in text
    assert "1-50" not in text
    review_skill = Path(__file__).resolve().parents[1] / "src/axiom/dispatcher/skills/redteam-review/SKILL.md"
    assert "先读 `redteam-scoring`" in review_skill.read_text(encoding="utf-8")
