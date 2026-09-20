"""运行目录清理全覆盖：五类 Worker 失败即删 + 完成即删。

每个用例先经 dispatch 接口推进真实生命周期并物化运行目录（含标记文件），
触发释放/提交动作后断言目录被删除；``sandboxes=None`` 的服务实例则断言
清理被跳过且不抛异常。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, update

from axiom.modules.axiom_tools.service import AxiomToolService
from axiom.modules.context.service import ContextService
from axiom.modules.execution.schemas import ReleaseIntentCommand
from axiom.modules.execution.service import ExecutionService
from axiom.modules.phase_capabilities.service import PhaseCapabilityService
from axiom.modules.plan.service import PlanService
from axiom.modules.reason.service import ReasonService
from axiom.modules.reason.tables import reason_runs, reason_workspaces
from axiom.modules.skills.service import SkillService
from axiom.modules.vulnerability_reviews.service import VulnerabilityReviewService
from axiom.shared.clock import utcnow
from axiom.shared.storage import FileStore
from protocol_helpers import (
    claim_reason_run,
    complete_bootstrap,
    open_reason_workspace,
    update_runtime_settings,
)

BOOTSTRAP_FILES = {
    "asset_inventory.md": "# 资产\n\n- a.example.com",
    "initial_leads.md": "# 线索\n\n- 验证对象级授权",
    "findings.md": "# 已确认问题\n\n- 本轮无确认漏洞",
}


# ---------------------------------------------------------------------------
# 生命周期构造辅助
# ---------------------------------------------------------------------------
def _register_worker(client) -> None:
    response = client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore", "plan"], "max_running": 8},
    )
    assert response.status_code == 200, response.text


def _new_task(client, *, title: str) -> dict:
    project = client.post("/api/v1/projects", json={"title": title}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": title,
            "origin": "a.example.com",
            "goal": "覆盖攻击面并验证高价值方向",
            "worker_pool": ["worker-a"],
        },
    ).json()


def _claim_and_start_intent(client, task_id: str, intent_id: str = "i001") -> str:
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
        json={"worker_name": "worker-a"},
    )
    assert claim.status_code == 200, claim.text
    lease_id = claim.json()["lease_id"]
    started = client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/start",
        json={"lease_id": lease_id},
    )
    assert started.status_code == 200, started.text
    return lease_id


def _release_intent(client, task_id: str, intent_id: str, lease_id: str, *, failure_class: str):
    return client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/release",
        json={"lease_id": lease_id, "failure_kind": "model_error", "failure_class": failure_class},
    )


def _bootstrap_to_reason(client) -> tuple[dict, dict]:
    _register_worker(client)
    task = _new_task(client, title="运行目录清理")
    lease_id = _claim_and_start_intent(client, task["id"])
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=lease_id,
        files=BOOTSTRAP_FILES,
    )
    return task, completed


def _finish_reason_run(engine, task_id: str, reason_run_id: str) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task_id, reason_runs.c.id == reason_run_id)
            .values(status="completed", concluded_at=now, updated_at=now)
        )


def _prepare_plan_run(client, engine, task: dict, completed: dict) -> tuple[str, str, dict]:
    """结束 Bootstrap 触发的 ReasonRun，经 reconcile 产生 PlanRun 并进入执行态。"""
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    reconciled = client.post("/api/v1/dispatch/plan-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    [plan_run_id] = reconciled.json()["created_plan_run_ids"]
    base = f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan_run_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
    assert claim.status_code == 200, claim.text
    lease_id = claim.json()["lease_id"]
    started = client.post(f"{base}/start", json={"lease_id": lease_id})
    assert started.status_code == 200, started.text
    execution = client.post(f"{base}/input", json={"lease_id": lease_id})
    assert execution.status_code == 200, execution.text
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": plan_run_id,
            "lease_id": lease_id,
            "context_revision": execution.json()["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    return plan_run_id, lease_id, workspace.json()


def _todo(number: int, fact_id: str) -> dict:
    return {
        "ref": f"todo-{number}",
        "title": f"验证测试方向 {number}",
        "category": "authorization",
        "sources": [{"type": "fact", "id": fact_id}],
    }


def _proposal(number: int, fact_id: str) -> dict:
    return {
        "todo_ref": f"todo-{number}",
        "from_facts": [fact_id],
        "source_refs": [f"fact:{fact_id}"],
        "description": f"验证接口方向 {number} 的授权边界",
        "why_now": f"Plan 识别到接口方向 {number} 尚未覆盖",
        "expected_signal": "出现越权或敏感数据返回",
        "stop_condition": "确认边界后停止该向量",
        "targets": ["a.example.com"],
        "execute_seconds": 300,
        "conclude_seconds": 180,
    }


def _plan_proposal(number: int, fact_id: str) -> dict:
    proposal = _proposal(number, fact_id)
    proposal["description"] = (
        "why_now: 历史覆盖存在缺口; endpoint: /api/orders/{id}; "
        "test_vector: cross-org-id-swap; expected_signal: 返回他人订单; "
        "stop_condition: 三个对象均拒绝访问"
    )
    return proposal


def _draft_reason_workspace(client, task: dict, completed: dict) -> tuple[dict, dict, str, str]:
    """认领 ReasonRun、打开 Workspace 并写入可提交的完整草稿。"""
    reason_run_id = completed["next_reason_run_id"]
    claim, _execution = claim_reason_run(client, task_id=task["id"], reason_run_id=reason_run_id)
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = f"/api/v1/reason-workspaces/{workspace['id']}"
    todo = client.post(
        f"{base}/todos",
        json={"lease_id": claim["lease_id"], "todo": _todo(1, completed["fact_id"])},
    )
    assert todo.status_code == 200, todo.text
    proposal = client.post(
        f"{base}/intents",
        json={"lease_id": claim["lease_id"], "intent": _proposal(1, completed["fact_id"])},
    )
    assert proposal.status_code == 200, proposal.text
    assessment = client.post(
        f"{base}/goal-assessment",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "当前证据不足，继续执行已规划的 Explore。",
                "evidence_refs": [f"fact:{completed['fact_id']}"],
            },
        },
    )
    assert assessment.status_code == 200, assessment.text
    return reason_run_id, workspace, claim["lease_id"], base


def _review_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Reviewer 运行目录"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Reviewer 运行目录",
            "origin": "review.example.com",
            "goal": "验证漏洞审核链路",
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/reviewer-a",
        json={"capabilities": ["review"], "max_running": 4, "config": {"model": "review-model"}},
    )
    assert worker.status_code == 200, worker.text
    return task


def _vulnerability(client, task_id: str, **overrides) -> dict:
    payload = {
        "title": "对象级授权缺失",
        "type": "越权访问",
        "severity": "high",
        "description": "低权限账号可以读取其他用户对象。",
        "prerequisites": "需要任意低权限账号。",
        "impact": "造成跨用户数据泄露。",
        "remediation": "服务端校验对象归属。",
        "affected_target": "https://review.example.com/api/orders/2",
        "source": "human",
        "reported_by": "tester",
        "finding_artifact_id": None,
        "evidence_artifact_ids": [],
    }
    payload.update(overrides)
    response = client.post(f"/api/v1/tasks/{task_id}/vulnerabilities", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _first_review_run(client, task_id: str, vulnerability_id: str) -> dict:
    runs = client.get(
        f"/api/v1/tasks/{task_id}/vulnerabilities/{vulnerability_id}/reviews"
    ).json()
    assert runs, "报告漏洞后应自动产生 ReviewRun"
    return runs[0]


def _claim_review(client, task_id: str, run_id: str) -> str:
    base = f"/api/v1/dispatch/tasks/{task_id}/vulnerability-review-runs/{run_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "reviewer-a"})
    assert claim.status_code == 200, claim.text
    lease_id = claim.json()["lease_id"]
    started = client.post(f"{base}/start", json={"lease_id": lease_id})
    assert started.status_code == 200, started.text
    return lease_id


def _write_review_outputs(run_dir: Path) -> None:
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "review.md").write_text(
        "# 审核结论\n\n现有证据能够证明对象级授权缺失。\n",
        encoding="utf-8",
    )
    evidence_dir = artifacts / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "minimal-retest.txt").write_text(
        "GET /api/orders/2 -> 200\n",
        encoding="utf-8",
    )


def _upload(client, task_id: str, name: str, content: str) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/artifacts/upload",
        files={"file": (name, content.encode(), "text/plain")},
        data={"actor": "human", "summary": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _materialize(client, task_id: str, owner_id: str, lease_id: str) -> Path:
    """用应用容器同款 SandboxesService 物化运行目录并放置标记文件。"""
    manager = client.app.state.container.sandboxes
    run_dir = manager.run_directory(task_id=task_id, owner_id=owner_id, lease_id=lease_id)
    (run_dir / "marker.txt").write_text("run-dir", encoding="utf-8")
    return run_dir


# ---------------------------------------------------------------------------
# 失败即删：Intent 释放
# ---------------------------------------------------------------------------
def test_intent_release_removes_run_directory_non_terminal(client) -> None:
    _register_worker(client)
    task = _new_task(client, title="Intent 释放清理")
    lease_id = _claim_and_start_intent(client, task["id"])
    run_dir = _materialize(client, task["id"], "i001", lease_id)
    assert run_dir.is_dir()

    response = _release_intent(client, task["id"], "i001", lease_id, failure_class="execution")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert not run_dir.exists()


def test_intent_release_preserves_evidence_and_review_artifacts(client) -> None:
    _register_worker(client)
    task = _new_task(client, title="证据抢救清理")
    lease_id = _claim_and_start_intent(client, task["id"])
    run_dir = _materialize(client, task["id"], "i001", lease_id)
    evidence = run_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "open-redirect.txt").write_text("GET /toggle-login?rdUri=https://evil.test\n", encoding="utf-8")
    review_artifacts = run_dir / "artifacts"
    review_artifacts.mkdir(parents=True, exist_ok=True)
    (review_artifacts / "review.md").write_text("# 审核结论\n", encoding="utf-8")

    response = _release_intent(client, task["id"], "i001", lease_id, failure_class="execution")
    assert response.status_code == 200, response.text
    assert not run_dir.exists()
    manager = client.app.state.container.sandboxes
    preserved = manager.store.root / "tasks" / task["id"] / "evidence" / "i001" / lease_id
    assert (preserved / "evidence" / "open-redirect.txt").read_text(encoding="utf-8").startswith("GET /toggle-login")
    assert (preserved / "artifacts" / "review.md").read_text(encoding="utf-8") == "# 审核结论\n"


def test_intent_release_removes_run_directory_when_terminal(client) -> None:
    update_runtime_settings(client, execution_retry_limit=1)
    _register_worker(client)
    task = _new_task(client, title="Intent 终态清理")
    lease_id = _claim_and_start_intent(client, task["id"])
    run_dir = _materialize(client, task["id"], "i001", lease_id)

    response = _release_intent(client, task["id"], "i001", lease_id, failure_class="execution")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert not run_dir.exists()


def test_intent_release_removes_run_directory_when_transient_retry_blocked(client) -> None:
    update_runtime_settings(client, infrastructure_retry_limit=1)
    _register_worker(client)
    task = _new_task(client, title="Intent 封锁清理")
    lease_id = _claim_and_start_intent(client, task["id"])
    run_dir = _materialize(client, task["id"], "i001", lease_id)

    response = _release_intent(client, task["id"], "i001", lease_id, failure_class="transient")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["next_retry_at"] is None
    assert not run_dir.exists()


# ---------------------------------------------------------------------------
# 失败即删：Reason / Plan / Review 释放
# ---------------------------------------------------------------------------
def test_reason_release_run_removes_run_directory_and_preserves_workspace_draft(
    client, engine
) -> None:
    task, completed = _bootstrap_to_reason(client)
    reason_run_id, workspace, lease_id, _base = _draft_reason_workspace(client, task, completed)
    run_dir = _materialize(client, task["id"], reason_run_id, lease_id)
    with engine.begin() as conn:
        draft_before = conn.execute(
            select(reason_workspaces.c.draft_json).where(
                reason_workspaces.c.id == workspace["id"]
            )
        ).scalar_one()

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}/release",
        json={
            "lease_id": lease_id,
            "failure_kind": "reason_plan_invalid",
            "failure_class": "execution",
        },
    )

    assert response.status_code == 200, response.text
    assert not run_dir.exists()
    with engine.begin() as conn:
        draft_after = conn.execute(
            select(reason_workspaces.c.draft_json).where(
                reason_workspaces.c.id == workspace["id"]
            )
        ).scalar_one()
    assert draft_after == draft_before


def test_plan_release_run_removes_run_directory(client, engine) -> None:
    task, completed = _bootstrap_to_reason(client)
    plan_run_id, lease_id, _workspace = _prepare_plan_run(client, engine, task, completed)
    run_dir = _materialize(client, task["id"], plan_run_id, lease_id)

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan_run_id}/release",
        json={"lease_id": lease_id, "failure_kind": "model_error", "failure_class": "execution"},
    )

    assert response.status_code == 200, response.text
    assert not run_dir.exists()


def test_review_release_run_removes_run_directory(client) -> None:
    task = _review_task(client)
    vulnerability = _vulnerability(client, task["id"])
    run = _first_review_run(client, task["id"], vulnerability["id"])
    lease_id = _claim_review(client, task["id"], run["id"])
    run_dir = _materialize(client, task["id"], run["id"], lease_id)

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{run['id']}/release",
        json={"lease_id": lease_id, "failure_kind": "model_error", "failure_class": "execution"},
    )

    assert response.status_code == 200, response.text
    assert not run_dir.exists()


# ---------------------------------------------------------------------------
# 完成即删：Reason / Plan / Review
# ---------------------------------------------------------------------------
def test_reason_commit_removes_run_directory(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    reason_run_id, _workspace, lease_id, base = _draft_reason_workspace(client, task, completed)
    run_dir = _materialize(client, task["id"], reason_run_id, lease_id)

    committed = client.post(f"{base}/commit", json={"lease_id": lease_id})

    assert committed.status_code == 200, committed.text
    assert committed.json()["created_intent_ids"]
    assert not run_dir.exists()


def test_plan_commit_removes_run_directory(client, engine) -> None:
    task, completed = _bootstrap_to_reason(client)
    plan_run_id, lease_id, workspace = _prepare_plan_run(client, engine, task, completed)
    base = f"/api/v1/plan-workspaces/{workspace['id']}"
    todo = client.post(
        f"{base}/todos",
        json={"lease_id": lease_id, "todo": _todo(1, completed["fact_id"])},
    )
    assert todo.status_code == 200, todo.text
    proposal = client.post(
        f"{base}/intents",
        json={"lease_id": lease_id, "intent": _plan_proposal(1, completed["fact_id"])},
    )
    assert proposal.status_code == 200, proposal.text
    decision = client.post(
        f"{base}/decision",
        json={
            "lease_id": lease_id,
            "decision": {"decision": "dispatch_explore", "reviewed_dimensions": ["coverage"]},
        },
    )
    assert decision.status_code == 200, decision.text
    run_dir = _materialize(client, task["id"], plan_run_id, lease_id)

    committed = client.post(f"{base}/commit", json={"lease_id": lease_id})

    assert committed.status_code == 200, committed.text
    assert committed.json()["created_intent_ids"]
    assert not run_dir.exists()


def test_review_complete_removes_run_directory(client) -> None:
    task = _review_task(client)
    finding = _upload(client, task["id"], "findings.md", "# Finding\n\nConfirmed IDOR")
    evidence = _upload(client, task["id"], "response.txt", "HTTP/1.1 200 OK")
    vulnerability = _vulnerability(
        client,
        task["id"],
        finding_artifact_id=finding["id"],
        evidence_artifact_ids=[evidence["id"]],
    )
    run = _first_review_run(client, task["id"], vulnerability["id"])
    base = f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{run['id']}"
    lease_id = _claim_review(client, task["id"], run["id"])
    execution = client.post(f"{base}/input", json={"lease_id": lease_id})
    assert execution.status_code == 200, execution.text
    run_dir = Path(execution.json()["run_dir"])
    _write_review_outputs(run_dir)
    decision = client.post(
        f"{base}/decision",
        json={
            "lease_id": lease_id,
            "decision": "verified",
            "reason": "审核证据与漏洞声明一致，结论具备可复核依据。",
            "duplicate_of": None,
        },
    )
    assert decision.status_code == 200, decision.text

    completed = client.post(f"{base}/complete", json={"lease_id": lease_id})

    assert completed.status_code == 200, completed.text
    assert completed.json()["vulnerability_status"] == "已验证"
    assert not run_dir.exists()


# ---------------------------------------------------------------------------
# sandboxes=None：清理跳过且不抛异常
# ---------------------------------------------------------------------------
def _none_execution_service(engine, data_dir: Path) -> ExecutionService:
    store = FileStore(data_dir)
    skills = SkillService(data_dir, engine=engine)
    phase_capabilities = PhaseCapabilityService(
        engine, data_dir, skills=skills, initialize_defaults=False
    )
    return ExecutionService(
        engine,
        store,
        context=ContextService(engine, store),
        reason=ReasonService(engine, store=store),
        skills=skills,
        axiom_tools=AxiomToolService(
            engine,
            data_dir,
            skills=skills,
            phase_capabilities=phase_capabilities,
        ),
        reviews=VulnerabilityReviewService(engine, store),
        sandboxes=None,
    )


def test_intent_release_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    _register_worker(client)
    task = _new_task(client, title="无沙箱 Intent 释放")
    lease_id = _claim_and_start_intent(client, task["id"])
    run_dir = _materialize(client, task["id"], "i001", lease_id)

    released = _none_execution_service(engine, data_dir).release(
        task_id=task["id"],
        intent_id="i001",
        command=ReleaseIntentCommand(
            lease_id=lease_id, failure_kind="model_error", failure_class="execution"
        ),
    )

    assert released.status == "pending"
    assert run_dir.is_dir()


def test_reason_release_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    task, completed = _bootstrap_to_reason(client)
    reason_run_id = completed["next_reason_run_id"]
    claim, _execution = claim_reason_run(
        client, task_id=task["id"], reason_run_id=reason_run_id
    )
    run_dir = _materialize(client, task["id"], reason_run_id, claim["lease_id"])

    released = ReasonService(engine, store=FileStore(data_dir), sandboxes=None).release_run(
        task["id"],
        reason_run_id,
        lease_id=claim["lease_id"],
        failure_kind="model_error",
        failure_class="execution",
    )

    assert released.status == "pending"
    assert run_dir.is_dir()


def test_plan_release_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    task, completed = _bootstrap_to_reason(client)
    plan_run_id, lease_id, _workspace = _prepare_plan_run(client, engine, task, completed)
    run_dir = _materialize(client, task["id"], plan_run_id, lease_id)

    released = PlanService(engine, sandboxes=None).release_run(
        task["id"],
        plan_run_id,
        lease_id=lease_id,
        failure_kind="model_error",
        failure_class="execution",
    )

    assert released.status == "pending"
    assert run_dir.is_dir()


def test_review_release_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    task = _review_task(client)
    vulnerability = _vulnerability(client, task["id"])
    run = _first_review_run(client, task["id"], vulnerability["id"])
    lease_id = _claim_review(client, task["id"], run["id"])
    run_dir = _materialize(client, task["id"], run["id"], lease_id)

    released = VulnerabilityReviewService(
        engine, FileStore(data_dir), sandboxes=None
    ).release_run(
        task["id"],
        run["id"],
        lease_id=lease_id,
        failure_kind="model_error",
        failure_class="execution",
    )

    assert released.status == "pending"
    assert run_dir.is_dir()


def test_reason_commit_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    task, completed = _bootstrap_to_reason(client)
    reason_run_id, workspace, lease_id, _base = _draft_reason_workspace(client, task, completed)
    run_dir = _materialize(client, task["id"], reason_run_id, lease_id)

    committed = ReasonService(engine, store=FileStore(data_dir), sandboxes=None).commit(
        workspace["id"], lease_id=lease_id
    )

    assert committed.created_intent_ids
    assert run_dir.is_dir()


def test_plan_commit_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    task, completed = _bootstrap_to_reason(client)
    plan_run_id, lease_id, workspace = _prepare_plan_run(client, engine, task, completed)
    base = f"/api/v1/plan-workspaces/{workspace['id']}"
    todo = client.post(
        f"{base}/todos",
        json={"lease_id": lease_id, "todo": _todo(1, completed["fact_id"])},
    )
    assert todo.status_code == 200, todo.text
    proposal = client.post(
        f"{base}/intents",
        json={"lease_id": lease_id, "intent": _plan_proposal(1, completed["fact_id"])},
    )
    assert proposal.status_code == 200, proposal.text
    decision = client.post(
        f"{base}/decision",
        json={
            "lease_id": lease_id,
            "decision": {"decision": "dispatch_explore", "reviewed_dimensions": ["coverage"]},
        },
    )
    assert decision.status_code == 200, decision.text
    run_dir = _materialize(client, task["id"], plan_run_id, lease_id)

    committed = PlanService(engine, sandboxes=None).commit(
        workspace["id"], lease_id=lease_id
    )

    assert committed.created_intent_ids
    assert run_dir.is_dir()


def test_review_complete_without_sandboxes_keeps_run_directory(
    client, engine, data_dir
) -> None:
    task = _review_task(client)
    finding = _upload(client, task["id"], "findings.md", "# Finding\n\nConfirmed IDOR")
    evidence = _upload(client, task["id"], "response.txt", "HTTP/1.1 200 OK")
    vulnerability = _vulnerability(
        client,
        task["id"],
        finding_artifact_id=finding["id"],
        evidence_artifact_ids=[evidence["id"]],
    )
    run = _first_review_run(client, task["id"], vulnerability["id"])
    base = f"/api/v1/dispatch/tasks/{task['id']}/vulnerability-review-runs/{run['id']}"
    lease_id = _claim_review(client, task["id"], run["id"])
    legacy_dir = FileStore(data_dir).run_directory(
        task_id=task["id"], review_run_id=run["id"], lease_id=lease_id
    )
    _write_review_outputs(legacy_dir)
    submitted = client.post(
        f"{base}/decision",
        json={
            "lease_id": lease_id,
            "decision": "verified",
            "reason": "审核证据与漏洞声明一致，结论具备可复核依据。",
            "duplicate_of": None,
        },
    )
    assert submitted.status_code == 200, submitted.text

    completed = VulnerabilityReviewService(
        engine, FileStore(data_dir), sandboxes=None
    ).complete(task_id=task["id"], run_id=run["id"], lease_id=lease_id)

    assert completed.vulnerability_status == "已验证"
    assert legacy_dir.is_dir()
