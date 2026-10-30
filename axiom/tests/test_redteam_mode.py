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

# 红队成果必须带结构化复现（报文逐字真实，不是 curl）。各用例只关心审核/计分规则，
# 所以复现内容取一份最小但合法的样本。
REPRODUCTION = {
    "entry_point": "POST /api/member/list",
    "prerequisites": ["持有普通成员会话"],
    "chain_input": "普通成员会话",
    "chain_gain": "可读取成员列表",
    "steps": [
        {
            "title": "用普通成员会话调用成员管理接口",
            "request": (
                "POST /api/member/list HTTP/1.1\r\n"
                "Host: a.example.com\r\n"
                "Cookie: sid=member-session\r\n"
                "Content-Type: application/json\r\n"
                "\r\n"
                '{"page":1}'
            ),
            "response": 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"items":[{"id":1}]}',
            "proves": "普通成员可直接读取成员列表，越权成立",
        }
    ],
}

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


def _attach_packet_shots(client, task_id: str, result_id: str, steps: int = 1) -> None:
    """Satisfy the packet-shot gate the way a reviewer would.

    Every reproduction step needs its own response screenshot before a verdict can be
    filed; these lifecycle tests care about the verdict, so they hang the minimum.
    """
    for step in range(1, steps + 1):
        response = client.post(
            f"/api/v1/tasks/{task_id}/red-team/results/{result_id}/evidence/capture",
            files={"file": (f"step-{step:02d}.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            data={"step_index": str(step), "kind": "packet", "caption": f"步骤 {step} 回包"},
        )
        assert response.status_code == 201, response.text


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
        "reproduction": REPRODUCTION,
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


def test_redteam_goal_text_follows_target_points(client) -> None:
    """红队目标文案固定，只有积分可变量；文案由 target_points 派生。"""

    project = client.post("/api/v1/projects", json={"title": "固定目标文案"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "红队分值",
            "origin": "a.example.com",
            # 调用方给什么文案都会被忽略：红队目标按 target_points 重写
            "goal": "随便写一个目标",
            "worker_pool": ["worker-a"],
            "engagement_mode": "red_team",
            "red_team_objective": {
                "initial_principal": {"id": "user:initial", "label": "初始身份"},
                "target_points": 250,
            },
        },
    )
    assert task.status_code == 201, task.text
    assert task.json()["goal"] == "对该资产进行渗透测试，并取得250积分"
    assert task.json()["red_team_objective"]["target_points"] == 250

    # 漏洞模式仍然是自由文案
    vulnerability = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "漏洞任务",
            "origin": "a.example.com",
            "goal": "挖 3 个高危漏洞",
            "worker_pool": ["worker-a"],
        },
    )
    assert vulnerability.status_code == 201, vulnerability.text
    assert vulnerability.json()["goal"] == "挖 3 个高危漏洞"


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
            "goal": "对该资产进行渗透测试，并取得100积分",
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


def test_chain_ids_are_scoped_to_their_task(client) -> None:
    """每条任务自成一套 ch001 起编号，不沿用全局计数器。"""

    first, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=first["id"], claim=claim)
    draft = _chain_draft(completed)
    second_chain = dict(draft["chain_adds"][0])
    second_chain["ref"] = "ch-2"
    second_chain["title"] = "第二条链"
    draft["chain_adds"].append(second_chain)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], draft
    )
    assert committed.status_code == 200, committed.text
    # 同任务内就地递增
    assert committed.json()["created_chain_ids"] == ["ch001", "ch002"]

    # 另一个任务从头开始，不受前一个任务影响
    other, other_claim, other_completed = _active_reason(client, red_team=True)
    other_ws = open_reason_workspace(client, task_id=other["id"], claim=other_claim)
    other_commit = apply_reason_draft(
        client, other_ws["id"], other_claim["lease_id"], _chain_draft(other_completed)
    )
    assert other_commit.status_code == 200, other_commit.text
    assert other_commit.json()["created_chain_ids"] == ["ch001"]


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
    # 链编号按任务独立（ch001 起），与 intent/fact/artifact 同一套口径
    assert chain_id == "ch001"
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
        "reproduction": REPRODUCTION,
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
        "reproduction": REPRODUCTION,
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
    # 裁决前必须给每一步挂一张报文图（审核闸门）
    _attach_packet_shots(client, task["id"], result_id, steps=len(REPRODUCTION["steps"]))
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
        "reproduction": REPRODUCTION,
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
    # 成果编号按任务独立（x001 起），与漏洞的 v001 同一套口径
    assert item["id"] == "x001"
    # 证据链按产物原文件名返回，供控制台直接展示
    assert [artifact["name"] for artifact in item["evidence_artifacts"]] == ["evidence.txt"]

    filtered = client.get(
        "/api/v1/red-team/results",
        params={"task_id": task["id"], "status": "reviewing", "type": "permission"},
    )
    assert filtered.headers["X-Total-Count"] == "1"
    by_search = client.get("/api/v1/red-team/results", params={"search": "成员管理"})
    assert by_search.headers["X-Total-Count"] == "1"
    by_status = client.get("/api/v1/red-team/results", params={"status": "verified"})
    assert by_status.headers["X-Total-Count"] == "0"
    # 核定分是筛选集的全量聚合：还没有 verified 时必须是 0，而不是缺头。
    assert by_status.headers["X-Total-Points"] == "0"

    chains = client.get("/api/v1/red-team/chains")
    assert chains.status_code == 200, chains.text
    assert chains.headers["X-Total-Count"] == "1"
    assert chains.json()[0]["id"] == chain_id


def test_cross_task_result_paging_keeps_the_full_awarded_total(client, engine) -> None:
    """翻页只改返回的行，不改核定总分。

    控制台的「核定 X 分」曾经是对本页 items 求和得出的；加上分页以后那样会
    变成「本页核定多少分」，翻到第 2 页数字就掉一截。这条测试锁住：
    X-Total-Points 必须覆盖整个筛选集。
    """

    from sqlalchemy import insert

    from axiom.modules.redteam.tables import red_team_chains, red_team_results

    project = client.post("/api/v1/projects", json={"title": "分页"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "分页任务",
            "origin": "b.example.com",
            "goal": "对该资产进行渗透测试，并取得100积分",
            "worker_pool": ["worker-a"],
            "engagement_mode": "red_team",
        },
    ).json()

    now = "2026-10-14T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            insert(red_team_chains).values(
                id="chPAGE01", task_id=task["id"], title="链",
                initial_principal_json={"id": "user:initial", "label": "初始身份", "tenant": "", "credential_id": None},
                target_capabilities_json=[], target_assets_json=[], current_state_json={},
                source_fact_ids_json=[], source_intent_ids_json=[],
                status="open", created_by="worker.reason", created_at=now, updated_at=now,
            )
        )
        for index, points in enumerate([30, 20, 10], start=1):
            # created_at 递减，让 DESC 排序固定成 rrPAGE01(30)、rrPAGE02(20)、rrPAGE03(10)，
            # 这样两页的本页求和才分别是 50 和 10，可以直接和全量 60 对比。
            stamp = f"2026-10-14T00:00:0{4 - index}Z"
            conn.execute(
                insert(red_team_results).values(
                    id=f"rrPAGE0{index}", task_id=task["id"], chain_id="chPAGE01", intent_id="i001",
                    result_type="permission", title=f"成果{index}", description="desc",
                    principal="user:initial", tenant="",
                    capabilities_before_json=[], capabilities_gained_json=["tenant:admin"],
                    capabilities_after_json=["tenant:admin"],
                    target_asset="member-management", data_target_id=None, sample_count=None,
                    sample_bytes=None, data_fields_json=[], data_summary="", credential_ref=None,
                    evidence_refs_json=[], status="verified", reported_by="worker-a",
                    reviewed_by="reviewer-a", review_reason="ok",
                    points_declared=points, points_awarded=points,
                    created_at=stamp, updated_at=stamp,
                )
            )

    first = client.get("/api/v1/red-team/results", params={"task_id": task["id"], "limit": 2})
    second = client.get(
        "/api/v1/red-team/results", params={"task_id": task["id"], "limit": 2, "offset": 2}
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.headers["X-Total-Count"] == "3"
    assert second.headers["X-Total-Count"] == "3"
    assert len(first.json()) == 2
    assert len(second.json()) == 1
    # 两页的行不相交 —— offset 生效，不是恒返回第一页
    assert {item["id"] for item in first.json()}.isdisjoint(
        {item["id"] for item in second.json()}
    )
    # 总分在两页上一致：30 + 20 + 10
    assert first.headers["X-Total-Points"] == "60"
    assert second.headers["X-Total-Points"] == "60"
    # 但本页求和只有 50 / 10 —— 正是不能拿来当总分的那个数
    assert sum(item["points_awarded"] for item in first.json()) == 50
    assert sum(item["points_awarded"] for item in second.json()) == 10

    # 筛选集缩小后总分跟着变，说明聚合用的是同一组 conditions
    verified_only = client.get(
        "/api/v1/red-team/results", params={"task_id": task["id"], "status": "reviewing"}
    )
    assert verified_only.headers["X-Total-Count"] == "0"
    assert verified_only.headers["X-Total-Points"] == "0"


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
                "reproduction": REPRODUCTION,
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
        reproduction=REPRODUCTION,
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


def test_redteam_notifications_fire_on_report_and_review(client) -> None:
    """红队成果上报与裁决各推送一次，卡片内容与库内数据一致。"""

    from unittest.mock import patch

    captured: list[tuple[str, str]] = []

    def fake_report(**kwargs):
        captured.append(("reported", kwargs["result_id"]))
        return True

    def fake_review(**kwargs):
        captured.append(("reviewed", kwargs["result_id"]))
        return True

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]
    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"proof")},
        data={"actor": "worker-a", "summary": "证据"},
    )
    artifact_id = uploaded.json()["id"]
    report = {
        "chain_id": chain_id,
        "result_type": "permission",
        "title": "取得成员管理能力",
        "description": "普通成员会话调用成员管理接口成功",
        "principal": "user:member",
        "capabilities_gained": ["tenant:admin"],
        "target_asset": "member-management",
        "evidence_refs": [f"artifact:{artifact_id}"],
        "reproduction": REPRODUCTION,
        "points_declared": 30,
    }

    with patch("axiom.shared.dingtalk.DingTalkNotifier.config",
               new=__import__("axiom.shared.dingtalk", fromlist=["DingTalkConfig"]).DingTalkConfig(
                   webhook_url="https://example.invalid/robot", enabled=True)), \
         patch("axiom.shared.dingtalk.DingTalkNotifier.notify_redteam_result_reported",
               side_effect=lambda **kw: fake_report(**kw)), \
         patch("axiom.shared.dingtalk.DingTalkNotifier.notify_redteam_result_reviewed",
               side_effect=lambda **kw: fake_review(**kw)):
        accepted = client.post(
            f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
            json={"lease_id": claim_body["lease_id"], "report": report},
        )
        assert accepted.status_code == 201, accepted.text
        result_id = accepted.json()["id"]
        assert captured == [("reported", result_id)]

        # Reviewer 直接调 API 裁决 → 第 2 次推送
        reviewed = client.post(
            f"/api/v1/tasks/{task['id']}/red-team/results/{result_id}/review",
            params={"reviewed_by": "reviewer.1"},
            json={"status": "verified", "reason": "证据可复现，四要素齐全", "points_awarded": 30},
        )
        assert reviewed.status_code == 200, reviewed.text

    assert captured == [("reported", result_id), ("reviewed", result_id)]


def test_redteam_notification_failure_never_breaks_business(client) -> None:
    """钉钉不可用不能回滚已落库的成果。"""

    from unittest.mock import patch

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]
    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"proof")},
        data={"actor": "worker-a", "summary": "证据"},
    )
    report = {
        "chain_id": chain_id,
        "result_type": "permission",
        "title": "取得成员管理能力",
        "description": "普通成员会话调用成员管理接口成功",
        "principal": "user:member",
        "capabilities_gained": ["tenant:admin"],
        "target_asset": "member-management",
        "evidence_refs": [f"artifact:{uploaded.json()['id']}"],
        "reproduction": REPRODUCTION,
        "points_declared": 30,
    }

    with patch("axiom.shared.dingtalk.DingTalkNotifier.config",
               new=__import__("axiom.shared.dingtalk", fromlist=["DingTalkConfig"]).DingTalkConfig(
                   webhook_url="https://example.invalid/robot", enabled=True)), \
         patch("axiom.shared.dingtalk.DingTalkNotifier.notify_redteam_result_reported",
               side_effect=RuntimeError("dingtalk down")):
        accepted = client.post(
            f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
            json={"lease_id": claim_body["lease_id"], "report": report},
        )

    assert accepted.status_code == 201, accepted.text
    result_id = accepted.json()["id"]
    listed = client.get(f"/api/v1/tasks/{task['id']}/red-team/results")
    assert [item["id"] for item in listed.json()] == [result_id]


def test_redteam_dingtalk_commands_list_and_detail(client) -> None:
    """群指令 /results 与 /result 能查到红队成果，且编号按任务内口径。"""

    from axiom.modules.integrations.dingtalk_api import route_dingtalk_command
    from axiom.shared.errors import NotFoundError

    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]
    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"proof")},
        data={"actor": "worker-a", "summary": "证据"},
    )
    accepted = client.post(
        f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
        json={
            "lease_id": claim_body["lease_id"],
            "report": {
                "chain_id": chain_id,
                "result_type": "permission",
                "title": "取得成员管理能力",
                "description": "普通成员会话调用成员管理接口成功",
                "principal": "user:member",
                "capabilities_gained": ["tenant:admin"],
                "target_asset": "member-management",
                "evidence_refs": [f"artifact:{uploaded.json()['id']}"],
                "reproduction": REPRODUCTION,
                "points_declared": 30,
            },
        },
    )
    result_id = accepted.json()["id"]
    assert result_id == "x001"

    container = client.app.state.container
    listed = route_dingtalk_command(container, f"/results {task['id']}")
    assert "成果清单" in listed["markdown"]["text"]
    assert result_id in listed["markdown"]["text"]
    assert "权限跃迁" in listed["markdown"]["text"]

    detail = route_dingtalk_command(container, f"/result {task['id']} {result_id}")
    assert result_id in detail["markdown"]["text"]
    assert "member-management" in detail["markdown"]["text"]

    missing = route_dingtalk_command(container, f"/result {task['id']} x999")
    assert "没有成果" in missing["markdown"]["text"]

    # /status 对红队任务显示成果数与积分，而不是「漏洞」
    status = route_dingtalk_command(container, f"/status {task['id']}")
    text = status["markdown"]["text"]
    assert "成果" in text
    assert "漏洞: `0`" not in text


def test_redteam_notification_fires_when_review_run_completes(client, engine) -> None:
    """Reviewer 走 run 完成路径（不是直连 API）时也必须推送裁决卡片。"""

    from sqlalchemy import select
    from unittest.mock import patch

    from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs

    captured: list[dict] = []
    task, claim, completed = _active_reason(client, red_team=True)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    committed = apply_reason_draft(
        client, workspace["id"], claim["lease_id"], _chain_draft(completed)
    )
    chain_id = committed.json()["created_chain_ids"][0]
    intent_id = committed.json()["created_intent_ids"][0]
    claim_body, _execution = _claim_explore(client, task["id"], intent_id)
    uploaded = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("evidence.txt", b"proof")},
        data={"actor": "worker-a", "summary": "证据"},
    )

    with patch("axiom.shared.dingtalk.DingTalkNotifier.config",
               new=__import__("axiom.shared.dingtalk", fromlist=["DingTalkConfig"]).DingTalkConfig(
                   webhook_url="https://example.invalid/robot", enabled=True)), \
         patch("axiom.shared.dingtalk.DingTalkNotifier.notify_redteam_result_reported",
               side_effect=lambda **kw: True), \
         patch("axiom.shared.dingtalk.DingTalkNotifier.notify_redteam_result_reviewed",
               side_effect=lambda **kw: captured.append(kw) or True):
        accepted = client.post(
            f"/api/v1/tasks/{task['id']}/intents/{intent_id}/red-team-results",
            json={
                "lease_id": claim_body["lease_id"],
                "report": {
                    "chain_id": chain_id,
                    "result_type": "permission",
                    "title": "取得成员管理能力",
                    "description": "普通成员会话调用成员管理接口成功",
                    "principal": "user:member",
                    "capabilities_gained": ["tenant:admin"],
                    "target_asset": "member-management",
                    "evidence_refs": [f"artifact:{uploaded.json()['id']}"],
                    "reproduction": REPRODUCTION,
                    "points_declared": 30,
                },
            },
        )
        result_id = accepted.json()["id"]
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
        client.post(
            f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}/release",
            json={"lease_id": claim_body["lease_id"], "failure_kind": "test_release"},
        )
        base = f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{review_run_id}"
        _attach_packet_shots(client, task["id"], result_id, steps=len(REPRODUCTION["steps"]))
        claimed = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
        lease = {"lease_id": claimed.json()["lease_id"]}
        client.post(f"{base}/start", json=lease)
        client.post(
            f"{base}/decision",
            json={**lease, "decision": "verified", "reason": "证据充分：权限跃迁可复现", "points_awarded": 30},
        )
        client.post(f"{base}/conclude", json=lease)
        finished = client.post(f"{base}/complete", json=lease)

    assert finished.status_code == 200, finished.text
    assert len(captured) == 1
    card = captured[0]
    assert card["result_id"] == result_id
    assert card["status"] == "verified"
    assert card["points_awarded"] == 30
    assert card["progress"] is not None and card["progress"][1] == 100


def _red_team_task(client, *, points: int = 500) -> dict:
    project = client.post("/api/v1/projects", json={"title": "目标积分"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "可调分值",
            "origin": "a.example.com",
            "worker_pool": ["worker-a"],
            "engagement_mode": "red_team",
            "red_team_objective": {
                "initial_principal": {"id": "user:initial", "label": "初始身份"},
                "target_points": points,
            },
        },
    )
    assert task.status_code == 201, task.text
    return task.json()


def test_update_task_target_points_rewrites_every_copy(client, engine) -> None:
    """改目标积分这一处，四处口径必须一起变，并留审计痕迹。

    积分同时是概览进度的分母、Goal 达成的门槛、注入给 Reason 的 Goal 事实，
    以及当前 Goal 周期的文案。只改其中一处，界面和目标校验就会各说各话。
    """

    from sqlalchemy import select as _select

    from axiom.modules.audit.tables import audit_events
    from axiom.modules.goals.tables import task_goal_cycles
    from axiom.modules.graph.tables import facts

    task = _red_team_task(client, points=500)

    updated = client.patch(f"/api/v1/tasks/{task['id']}", json={"target_points": 1000})
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["goal"] == "对该资产进行渗透测试，并取得1000积分"
    assert body["red_team_objective"]["target_points"] == 1000
    # 结构化目标里的其余字段不能被顺手清空
    assert body["red_team_objective"]["initial_principal"]["id"] == "user:initial"

    with engine.connect() as conn:
        goal_fact = conn.execute(
            _select(facts.c.description).where(
                facts.c.task_id == task["id"], facts.c.id == "goal"
            )
        ).scalar_one()
        cycle_goal = conn.execute(
            _select(task_goal_cycles.c.goal).where(
                task_goal_cycles.c.task_id == task["id"],
                task_goal_cycles.c.status == "active",
            )
        ).scalar_one()
        events = conn.execute(
            _select(audit_events.c.payload_json).where(
                audit_events.c.task_id == task["id"],
                audit_events.c.event_type == "task.goal_updated",
            )
        ).scalars().all()

    assert goal_fact == "对该资产进行渗透测试，并取得1000积分"
    assert cycle_goal == "对该资产进行渗透测试，并取得1000积分"
    assert events, "改积分必须留下 task.goal_updated 审计记录"
    import json as _json

    payload = _json.loads(events[-1]) if isinstance(events[-1], str) else events[-1]
    assert payload["previous_target_points"] == 500
    assert payload["target_points"] == 1000


def test_update_task_target_points_is_idempotent(client, engine) -> None:
    """同值提交不该产生噪声审计，也不该把 goal 重写一遍。"""

    from sqlalchemy import func as _func, select as _select

    from axiom.modules.audit.tables import audit_events

    task = _red_team_task(client, points=500)

    def count_goal_events() -> int:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    _select(_func.count()).select_from(audit_events).where(
                        audit_events.c.task_id == task["id"],
                        audit_events.c.event_type == "task.goal_updated",
                    )
                ).scalar_one()
            )

    before = count_goal_events()
    again = client.patch(f"/api/v1/tasks/{task['id']}", json={"target_points": 500})
    assert again.status_code == 200, again.text
    assert again.json()["red_team_objective"]["target_points"] == 500
    assert count_goal_events() == before


def test_update_task_target_points_rejects_bad_targets(client) -> None:
    task = _red_team_task(client, points=500)

    # 边界与 RedTeamObjective.target_points 一致
    assert client.patch(f"/api/v1/tasks/{task['id']}", json={"target_points": 0}).status_code == 422
    assert client.patch(f"/api/v1/tasks/{task['id']}", json={"target_points": 10001}).status_code == 422

    # 漏洞模式没有积分概念
    project = client.post("/api/v1/projects", json={"title": "漏洞"}).json()
    vuln = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "漏洞任务",
            "origin": "a.example.com",
            "goal": "挖漏洞",
            "worker_pool": ["worker-a"],
        },
    ).json()
    rejected = client.patch(f"/api/v1/tasks/{vuln['id']}", json={"target_points": 300})
    assert rejected.status_code == 409


def test_update_task_target_points_blocked_on_completed_task(client, engine) -> None:
    """已完成任务只能通过「继续任务」开新一轮 Goal，不能在原地改门槛。"""

    from sqlalchemy import update as _update

    task = _red_team_task(client, points=500)
    # 直接置为已完成：这条测试只关心 update 的守卫，不重演完整生命周期
    with engine.begin() as conn:
        conn.execute(
            _update(tasks).where(tasks.c.id == task["id"]).values(status="completed")
        )

    rejected = client.patch(f"/api/v1/tasks/{task['id']}", json={"target_points": 900})
    assert rejected.status_code == 409
