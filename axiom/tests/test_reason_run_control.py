from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from sqlalchemy import func, insert, select, update

from axiom.modules.execution.service import reason_prompt_profile_for_trigger
from axiom.modules.graph.tables import facts, intents
from axiom.modules.reason.tables import reason_runs, reason_workspaces
from axiom.modules.plan.tables import plan_runs
from axiom.modules.tasks.tables import tasks
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError
from protocol_helpers import (
    claim_reason_run,
    complete_bootstrap,
    open_reason_workspace,
    update_runtime_settings,
)


@pytest.mark.parametrize(
    ("trigger", "profile"),
    [
        ("bootstrap", "bootstrap"),
        ("fact_batch", "progress"),
        ("low_watermark", "progress"),
        ("max_wait", "progress"),
        ("intent_assessment", "progress"),
        ("manual", "human"),
        ("hint", "human"),
        ("continuation", "continuation"),
        ("future_trigger", "default"),
    ],
)
def test_reason_trigger_uses_one_code_selected_prompt_profile(trigger: str, profile: str) -> None:
    assert reason_prompt_profile_for_trigger(trigger) == profile


BOOTSTRAP_FILES = {
    "asset_inventory.md": "# 资产\n\n- a.example.com",
    "initial_leads.md": "# 线索\n\n- 验证对象级授权",
    "findings.md": "# 已确认问题\n\n- 本轮无确认漏洞",
}


def _bootstrap_to_reason(client, **runtime_changes) -> tuple[dict, dict]:
    if runtime_changes:
        update_runtime_settings(client, **runtime_changes)
    project = client.post("/api/v1/projects", json={"title": "ReasonRun"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "ReasonRun 控制面",
            "origin": "a.example.com",
            "goal": "覆盖攻击面并验证高价值方向",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore", "plan"], "max_running": 8},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": claim["lease_id"]}).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=claim["lease_id"],
        files=BOOTSTRAP_FILES,
    )
    return task, completed


def _active_workspace(client, task: dict, reason_run_id: str) -> tuple[dict, dict]:
    claim, execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=reason_run_id,
    )
    assert execution["intent_id"] is None
    assert execution["reason_run_id"] == reason_run_id
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    return claim, workspace


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


def _finish_reason_run(engine, task_id: str, reason_run_id: str) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task_id, reason_runs.c.id == reason_run_id)
            .values(status="completed", concluded_at=now, updated_at=now)
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(phase="waiting", updated_at=now)
        )


def _finish_plan_run(engine, task_id: str, plan_run_id: str) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task_id, plan_runs.c.id == plan_run_id)
            .values(status="completed", concluded_at=now, updated_at=now)
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(phase="waiting", updated_at=now)
        )


def _reconcile_plan(client, task_id: str) -> str:
    reconciled = client.post("/api/v1/dispatch/plan-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    created = reconciled.json()["created_plan_run_ids"]
    assert created, (reconciled.json(), client.get(f"/api/v1/tasks/{task_id}").json())
    [run_id] = created
    run = client.get(f"/api/v1/tasks/{task_id}/plan-runs/{run_id}").json()
    assert run["id"] == run_id
    return run_id


def _trigger_plan(client, task_id: str, *, trigger: str = "manual") -> str:
    response = client.post(
        f"/api/v1/tasks/{task_id}/plan-runs/trigger",
        json={"actor": "test", "trigger": trigger},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _active_plan_workspace(client, task: dict, plan_run_id: str) -> tuple[dict, dict, dict]:
    base = f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan_run_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "worker-a"})
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    started = client.post(f"{base}/start", json={"lease_id": claimed["lease_id"]})
    assert started.status_code == 200, started.text
    execution = client.post(f"{base}/input", json={"lease_id": claimed["lease_id"]})
    assert execution.status_code == 200, execution.text
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": plan_run_id,
            "lease_id": claimed["lease_id"],
            "context_revision": execution.json()["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    return claimed, execution.json(), workspace.json()


def _trigger_manual_reason(client, task_id: str, *, actor: str = "人工"):
    return client.post(
        f"/api/v1/tasks/{task_id}/reason-runs/trigger",
        json={"actor": actor},
    )


def _add_todo(client, workspace: dict, lease_id: str, todo: dict):
    return client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/todos",
        json={"lease_id": lease_id, "todo": todo},
    )


def _propose(client, workspace: dict, lease_id: str, proposal: dict):
    return client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intents",
        json={"lease_id": lease_id, "intent": proposal},
    )


def _add_plan_todo(client, workspace: dict, lease_id: str, todo: dict):
    return client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/todos",
        json={"lease_id": lease_id, "todo": todo},
    )


def _propose_plan(client, workspace: dict, lease_id: str, proposal: dict):
    return client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/intents",
        json={"lease_id": lease_id, "intent": proposal},
    )


def _assess_continue(client, workspace: dict, lease_id: str, fact_id: str) -> None:
    response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/goal-assessment",
        json={
            "lease_id": lease_id,
            "assessment": {
                "achieved": False,
                "reason": "当前证据不足，继续执行已规划的 Explore。",
                "evidence_refs": [f"fact:{fact_id}"],
            },
        },
    )
    assert response.status_code == 200, response.text


def _commit_plan(client, task: dict, completed: dict, *, count: int) -> list[str]:
    claim, workspace = _active_workspace(
        client,
        task,
        completed["next_reason_run_id"],
    )
    for number in range(1, count + 1):
        assert _add_todo(
            client, workspace, claim["lease_id"], _todo(number, completed["fact_id"])
        ).status_code == 200
        assert _propose(
            client,
            workspace,
            claim["lease_id"],
            _proposal(number, completed["fact_id"]),
        ).status_code == 200
    _assess_continue(client, workspace, claim["lease_id"], completed["fact_id"])
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    return committed.json()["created_intent_ids"]


def _closed_todo_terminal_explore_signal(
    client,
    *,
    reason_low_watermark: int = 0,
    reason_min_interval_seconds: int = 3_600,
) -> tuple[dict, str]:
    """Build the exact signal that must not be lost by Reason reconciliation.

    The Intent is terminal, its Todo is already completed, and there are no
    unplanned Facts.  The only remaining planning work is the one-time Intent
    assessment.  Keeping this setup in SQL avoids coupling this regression to
    the Explore artifact parser while still exercising the persisted lifecycle
    fields and the dispatcher-facing reconciliation path.
    """

    task, completed = _bootstrap_to_reason(
        client,
        reason_low_watermark=reason_low_watermark,
        reason_min_interval_seconds=reason_min_interval_seconds,
        reason_batch_size=5,
    )
    [intent_id] = _commit_plan(client, task, completed, count=1)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])

    todo = client.get(f"/api/v1/tasks/{task['id']}/todos").json()[0]
    closed = client.patch(
        f"/api/v1/tasks/{task['id']}/todos/{todo['id']}",
        json={"status": "completed", "status_reason": "该方向已覆盖"},
    )
    assert closed.status_code == 200, closed.text

    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == intent_id)
            .values(
                status="completed",
                worker=None,
                lease_id=None,
                last_heartbeat_at=None,
                concluded_at=now,
                to_fact_id=completed["fact_id"],
            )
        )
    return task, intent_id


def test_reason_run_is_unique_invisible_and_recovers_a_stale_lease(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    reason_run_id = completed["next_reason_run_id"]

    repeated = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert repeated.status_code == 200
    assert repeated.json()["created_reason_run_ids"] == []
    with client.app.state.container.engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(reason_runs).where(reason_runs.c.task_id == task["id"])
        ).scalar_one() == 1
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert all(item["kind"] != "reason" for item in graph["intents"])

    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=reason_run_id,
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}"
    assert client.post(f"{base}/heartbeat", json={"lease_id": claim["lease_id"]}).status_code == 204
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == reason_run_id)
            .values(last_heartbeat_at="2020-01-01T00:00:00Z")
        )

    recovered = client.post(
        "/api/v1/dispatch/recover-stale",
        json={"stale_after_seconds": 60},
    )

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["recovered_reason_runs"][0]["reason_run_id"] == reason_run_id
    run = client.get(f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}").json()
    assert run["status"] == "pending"
    assert run["infrastructure_retry_count"] == 1
    assert run["lease_id"] is None
    workspace_after = client.get(
        f"/api/v1/reason-workspaces/{workspace['id']}"
    ).json()
    assert workspace_after["status"] == "abandoned"
    assert workspace_after["failure_reason"] == "lease_expired"
    stale_heartbeat = client.post(f"{base}/heartbeat", json={"lease_id": claim["lease_id"]})
    assert stale_heartbeat.status_code == 409


def test_manual_reason_can_run_while_waiting_without_new_facts_and_reuses_reason_input(
    client,
) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])

    response = _trigger_manual_reason(client, task["id"], actor="Alice")

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["trigger"] == "manual"
    assert run["source_fact_ids"] == []
    assert run["trigger_payload"] == {
        "actor": "Alice",
        "requested_at": run["created_at"],
        "task_phase": "waiting",
        "unplanned_fact_count": 0,
        "source_fact_ids": [],
        "open_explore_count": 0,
        "open_explore_ids": [],
        "failed_intent_ids": [],
        "actionable_todo_ids": [],
        "todo_review_ids": [],
        "unassessed_intent_ids": [],
    }
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"

    claim, execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=run["id"],
    )
    assert execution["reason_run_id"] == run["id"]
    assert execution["intent_id"] is None
    assert execution["intent_kind"] == "reason"
    assert execution["intent_source_facts"] == []
    assert "人工 Alice 请求立即复盘当前任务" in execution["intent_description"]
    assert "允许新增 0 个 Intent" in execution["intent_description"]
    assert execution["reason_prompt_profile"] == "human"

    duplicate = _trigger_manual_reason(client, task["id"])
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] != run["id"]
    queued_after_duplicate = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert [item["id"] for item in queued_after_duplicate["queued_reason_runs"]] == [
        duplicate.json()["id"]
    ]

    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    assessment = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/goal-assessment",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "人工复盘后没有发现材料性的新方向",
                "evidence_refs": [],
            },
        },
    )
    assert assessment.status_code == 200, assessment.text
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["created_intent_ids"] == []
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "waiting"


def test_new_human_hint_immediately_triggers_reason_with_that_hint_as_reason(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])

    response = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "优先验证投诉撤销后是否仍能重复提交", "creator": "Alice"},
    )

    assert response.status_code == 201, response.text
    hint = response.json()
    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert current["active_reason_run"] is None
    [run] = current["queued_reason_runs"]
    assert run["trigger"] == "hint"
    assert run["trigger_payload"]["hint_id"] == hint["id"]
    assert run["trigger_payload"]["hint_content"] == hint["content"]
    assert run["trigger_payload"]["creator"] == "Alice"
    assert current["phase"] == "reason"

    _claim, execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=run["id"],
    )
    assert f"Hint {hint['id']}" in execution["intent_description"]
    assert hint["content"] in execution["intent_description"]
    assert "Hint 是人工规划依据而不是已验证 Fact" in execution["intent_description"]
    assert hint["content"] in execution["hints"]
    assert execution["reason_prompt_profile"] == "human"


def test_new_hint_is_saved_without_duplicate_reason_when_trigger_conditions_are_unmet(
    client,
) -> None:
    project = client.post("/api/v1/projects", json={"title": "Hint Trigger Guard"}).json()
    fresh = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Bootstrap 未完成",
            "origin": "fresh.example.com",
        },
    ).json()
    before_bootstrap = client.post(
        f"/api/v1/tasks/{fresh['id']}/hints",
        json={"content": "先确认登录入口", "creator": "人工"},
    )
    assert before_bootstrap.status_code == 201, before_bootstrap.text
    assert client.get(f"/api/v1/tasks/{fresh['id']}").json()["active_reason_run"] is None

    task, completed = _bootstrap_to_reason(client)
    active_id = completed["next_reason_run_id"]
    while_active = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "检查已有 Reason 是否会被重复创建", "creator": "人工"},
    )
    assert while_active.status_code == 201, while_active.text
    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert current["active_reason_run"] is None
    assert len(current["queued_reason_runs"]) == 2
    assert current["queued_reason_runs"][0]["id"] == active_id
    assert current["queued_reason_runs"][1]["trigger"] == "hint"
    with client.app.state.container.engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(reason_runs).where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.status.in_(("pending", "claimed", "running", "concluding")),
            )
        ).scalar_one()
    assert count == 2
    assert while_active.json() in client.get(f"/api/v1/tasks/{task['id']}/hints").json()


def test_manual_reason_snapshots_open_and_failed_explore_but_not_later_facts(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    created_intent_ids = _commit_plan(client, task, completed, count=2)
    engine = client.app.state.container.engine
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task["id"],
                intents.c.id == created_intent_ids[0],
            )
            .values(status="running", worker="worker-a", last_heartbeat_at=now)
        )
        conn.execute(
            update(intents)
            .where(
                intents.c.task_id == task["id"],
                intents.c.id == created_intent_ids[1],
            )
            .values(status="failed", concluded_at=now)
        )

    response = _trigger_manual_reason(client, task["id"])

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["trigger_payload"]["open_explore_ids"] == [created_intent_ids[0]]
    assert run["trigger_payload"]["failed_intent_ids"] == [created_intent_ids[1]]
    assert run["trigger_payload"]["open_explore_count"] == 1
    assert run["trigger_payload"]["unassessed_intent_ids"] == [created_intent_ids[1]]
    original_source_ids = list(run["source_fact_ids"])

    with engine.begin() as conn:
        later_fact_id = "f999"
        conn.execute(
            insert(facts).values(
                id=later_fact_id,
                task_id=task["id"],
                description="人工点击后才产生的新 Fact",
                created_at=utcnow(),
                archived=False,
            )
        )
    run_after = client.get(f"/api/v1/tasks/{task['id']}/reason-runs/{run['id']}").json()
    assert run_after["source_fact_ids"] == original_source_ids
    assert later_fact_id not in run_after["source_fact_ids"]


def test_manual_reason_rejects_unready_or_non_runnable_tasks(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Manual Reason Guard"}).json()
    fresh = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Bootstrap 未完成",
            "origin": "fresh.example.com",
            "goal": "完成安全测试",
        },
    ).json()
    not_bootstrapped = _trigger_manual_reason(client, fresh["id"])
    assert not_bootstrapped.status_code == 409
    assert "Bootstrap" in not_bootstrapped.text

    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    assert _trigger_manual_reason(client, task["id"]).status_code == 409

    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(
                status="running",
                phase="waiting",
                legacy_readonly=False,
                run_deadline_at="2000-01-01T00:00:00Z",
            )
        )
    assert _trigger_manual_reason(client, task["id"]).status_code == 409

    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(run_deadline_at=None, legacy_readonly=True)
        )
    readonly = _trigger_manual_reason(client, task["id"])
    assert readonly.status_code == 409
    assert "历史只读" in readonly.text


def test_manual_reason_bypasses_automatic_interval_and_fingerprint_suppression(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        reason_min_interval_seconds=3600,
    )
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])

    automatic = client.post("/api/v1/dispatch/plan-runs/reconcile", json={})
    assert automatic.status_code == 200, automatic.text
    [plan_run_id] = automatic.json()["created_plan_run_ids"]
    plan = client.get(
        f"/api/v1/tasks/{task['id']}/plan-runs/{plan_run_id}"
    ).json()
    assert plan["id"] == plan_run_id
    task_with_plan = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_with_plan["phase"] == "plan"
    assert task_with_plan["active_plan_run"] is None
    assert task_with_plan["active_reason_run"] is None

    manual = _trigger_manual_reason(client, task["id"])
    assert manual.status_code == 200, manual.text
    assert manual.json()["trigger"] == "manual"
    task_with_manual = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert [run["id"] for run in task_with_manual["queued_reason_runs"]] == [
        manual.json()["id"]
    ]


def test_plan_claim_uses_plan_context_and_no_work_enters_waiting(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    plan_run_id = _reconcile_plan(client, task["id"])

    claim, execution, workspace = _active_plan_workspace(client, task, plan_run_id)
    manifest = json.loads(Path(execution["context_manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["phase"] == "plan"
    assert {"goal_evaluations", "goal_cycles"} <= set(manifest["indexes"])

    workspace_id = workspace["id"]
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/decision",
        json={
            "lease_id": claim["lease_id"],
            "decision": {
                "decision": "exhausted",
                "reviewed_dimensions": ["coverage", "weaponization", "blocked_paths", "goal_completion"],
                "exhaustion_reason": "四个维度均已复核，没有材料性新方向",
                "confidence": "high",
            },
        },
    )
    assert committed.status_code == 200, committed.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text

    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "waiting"
    assert task_after["active_plan_run"] is None
    assert task_after["active_reason_run"] is None
    assert task_after["plan_run_count"] == 1
    assert task_after["last_plan_decision"]["plan_run_id"] == plan_run_id
    assert task_after["last_plan_decision"]["decision"] == "exhausted"
    plan_after = client.get(
        f"/api/v1/tasks/{task['id']}/plan-runs/{plan_run_id}"
    ).json()
    assert plan_after["decision"]["decision"] == "exhausted"
    assert client.post(
        "/api/v1/dispatch/plan-runs/reconcile", json={}
    ).json()["created_plan_run_ids"] == []


def test_plan_cannot_mark_goal_achieved(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    plan_run_id = _reconcile_plan(client, task["id"])
    claim, _execution, workspace = _active_plan_workspace(client, task, plan_run_id)

    response = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/decision",
        json={
            "lease_id": claim["lease_id"],
            "decision": {"decision": "achieved"},
        },
    )
    assert response.status_code == 422, response.text

    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "plan"
    assert task_after["status"] == "running"


def test_plan_commit_routes_new_intent_to_explore(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    plan_run_id = _reconcile_plan(client, task["id"])
    claim, _execution, workspace = _active_plan_workspace(client, task, plan_run_id)

    assert _add_plan_todo(
        client, workspace, claim["lease_id"], _todo(1, completed["fact_id"])
    ).status_code == 200
    proposal = _proposal(1, completed["fact_id"])
    proposal["description"] = (
        "why_now: 历史覆盖存在缺口; endpoint: /api/orders/{id}; "
        "test_vector: cross-org-id-swap; expected_signal: 返回他人订单; "
        "stop_condition: 三个对象均拒绝访问"
    )
    proposed = _propose_plan(client, workspace, claim["lease_id"], proposal)
    assert proposed.status_code == 200, proposed.text
    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/decision",
        json={
            "lease_id": claim["lease_id"],
            "decision": {
                "decision": "dispatch_explore",
                "reviewed_dimensions": ["coverage"],
            },
        },
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    assert len(committed.json()["created_intent_ids"]) == 1
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "explore"
    run = client.get(f"/api/v1/tasks/{task['id']}/plan-runs/{plan_run_id}").json()
    assert run["decision"]["decision"] == "dispatch_explore"


def test_plan_input_arriving_during_execution_hands_off_to_reason(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        reason_min_interval_seconds=3_600,
        reason_low_watermark=0,
    )
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    plan_run_id = _reconcile_plan(client, task["id"])
    claim, _execution, workspace = _active_plan_workspace(client, task, plan_run_id)

    with engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f999",
                task_id=task["id"],
                description="Plan 执行期间到达的新 Fact",
                created_at=utcnow(),
                archived=False,
            )
        )
    assessed = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/decision",
        json={
            "lease_id": claim["lease_id"],
            "decision": {
                "decision": "needs_reason",
                "reviewed_dimensions": ["coverage"],
                "exhaustion_reason": "Plan 期间出现新 Fact",
                "confidence": "medium",
            },
        },
    )
    assert assessed.status_code == 200, assessed.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    [reason_run_id] = reconciled["created_reason_run_ids"]
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}"
    ).json()
    assert run["trigger"] == "plan_handoff"
    assert run["source_fact_ids"] == ["f999"]


def test_pending_plan_does_not_cut_in_front_of_a_normal_reason_queue_entry(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    plan_run_id = _reconcile_plan(client, task["id"])
    manual = _trigger_manual_reason(client, task["id"], actor="Plan 后到达的人工输入")
    assert manual.status_code == 200, manual.text

    direct_plan_claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan_run_id}/claim",
        json={"worker_name": "worker-a"},
    )
    assert direct_plan_claim.status_code == 409
    assert "Reason" in direct_plan_claim.text

    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert [item["id"] for item in task_after["queued_reason_runs"]] == [manual.json()["id"]]
    assert task_after["active_plan_run"] is None


def test_plan_rejects_same_vector_when_only_an_unreferenced_new_fact_exists(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    first_plan_id = _reconcile_plan(client, task["id"])
    first_claim, _execution, first_workspace = _active_plan_workspace(client, task, first_plan_id)
    assert _add_plan_todo(client, first_workspace, first_claim["lease_id"], _todo(1, completed["fact_id"])).status_code == 200
    first_proposal = _proposal(1, completed["fact_id"])
    first_proposal["description"] = (
        "why_now: 首次验证; endpoint: /api/orders/{id}; "
        "test_vector: cross-org-id-swap; expected_signal: 200; stop_condition: 403"
    )
    first_proposal.update(
        endpoint="/api/orders/{id}",
        test_vector="cross-org-id-swap",
        expected_signal="200",
        stop_condition="403",
    )
    proposed = _propose_plan(client, first_workspace, first_claim["lease_id"], first_proposal)
    assert proposed.status_code == 200, proposed.text
    decision = client.post(
        f"/api/v1/plan-workspaces/{first_workspace['id']}/decision",
        json={"lease_id": first_claim["lease_id"], "decision": {"decision": "dispatch_explore", "reviewed_dimensions": ["coverage"]}},
    )
    assert decision.status_code == 200, decision.text
    first_commit = client.post(
        f"/api/v1/plan-workspaces/{first_workspace['id']}/commit",
        json={"lease_id": first_claim["lease_id"]},
    ).json()
    [intent_id] = first_commit["created_intent_ids"]
    [todo] = client.get(f"/api/v1/tasks/{task['id']}/todos").json()
    concluded_at = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == intent_id)
            .values(
                status="failed",
                assessment_status="completed",
                assessment_reason="已验证且失败",
                assessed_at=concluded_at,
                concluded_at=concluded_at,
            )
        )
    closed = client.patch(
        f"/api/v1/tasks/{task['id']}/todos/{todo['id']}",
        json={"status": "completed", "status_reason": "该向量已验证失败"},
    )
    assert closed.status_code == 200, closed.text
    manual = _trigger_manual_reason(client, task["id"], actor="Plan 去重间隔")
    assert manual.status_code == 200, manual.text
    _finish_reason_run(engine, task["id"], manual.json()["id"])

    second_plan_id = _trigger_plan(client, task["id"], trigger="manual_retry")
    second_claim, _execution, second_workspace = _active_plan_workspace(client, task, second_plan_id)
    assert _add_plan_todo(client, second_workspace, second_claim["lease_id"], _todo(2, completed["fact_id"])).status_code == 200
    with engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f999",
                task_id=task["id"],
                description="与订单向量无关的新 Fact",
                created_at=utcnow(),
                archived=False,
            )
        )
    repeated = _proposal(2, completed["fact_id"])
    repeated["description"] = (
        "why_now: 换一种表述重试; endpoint: /api/orders/{id}; "
        "test_vector: cross-org-id-swap; expected_signal: 数据泄露; stop_condition: 403"
    )
    repeated.update(
        endpoint="/api/orders/{id}",
        test_vector="cross-org-id-swap",
        expected_signal="数据泄露",
        stop_condition="403",
    )
    rejected = _propose_plan(client, second_workspace, second_claim["lease_id"], repeated)
    assert rejected.status_code == 409
    assert "没有新 Fact" in rejected.text


def test_plan_convergence_guard_stops_after_three_runs_and_resets_on_verified_vulnerability(
    client,
) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])

    for index in range(3):
        plan_run_id = _trigger_plan(client, task["id"], trigger=f"manual_cycle_{index + 1}")
        _finish_plan_run(engine, task["id"], plan_run_id)
        manual = _trigger_manual_reason(client, task["id"], actor=f"Plan 熔断测试 {index + 1}")
        assert manual.status_code == 200, manual.text
        _finish_reason_run(engine, task["id"], manual.json()["id"])

    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(phase="plan", updated_at=utcnow())
        )
    blocked = client.post("/api/v1/dispatch/plan-runs/reconcile", json={})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["created_plan_run_ids"] == []
    before_reset = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert before_reset["plan_run_count"] == 3
    assert before_reset["phase"] == "waiting"

    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(vulnerabilities).values(
                id="v001",
                task_id=task["id"],
                intent_id=None,
                title="已验证的回归漏洞",
                type="越权",
                severity="high",
                description="验证新的漏洞成果会重置 Plan 熔断计数",
                prerequisites="",
                impact="高",
                reproduction_steps_json="[]",
                remediation="",
                curl_command="",
                evidence_json="[]",
                affected_target="a.example.com",
                status="已验证",
                status_reason="测试用漏洞",
                source="test",
                reported_by="test",
                created_at=now,
                updated_at=now,
                archived=False,
                format_version=2,
            )
        )

    reset = client.post("/api/v1/dispatch/plan-runs/reconcile", json={})
    assert reset.status_code == 200, reset.text
    [reset_plan_id] = reset.json()["created_plan_run_ids"]
    reset_plan = client.get(
        f"/api/v1/tasks/{task['id']}/plan-runs/{reset_plan_id}"
    ).json()
    assert reset_plan["trigger"] == "reconcile"


def test_concurrent_manual_reason_requests_create_only_one_active_run(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    engine = client.app.state.container.engine
    _finish_reason_run(engine, task["id"], completed["next_reason_run_id"])
    service = client.app.state.container.reason
    barrier = Barrier(2)

    def trigger() -> str:
        barrier.wait()
        try:
            return service.trigger_manual(task["id"], actor="并发测试").id
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: trigger(), range(2)))

    assert results[0] == results[1]
    with engine.connect() as conn:
        active_count = conn.execute(
            select(func.count())
            .select_from(reason_runs)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.status.in_(["pending", "claimed", "running", "concluding"]),
            )
        ).scalar_one()
    assert active_count == 1


def test_stopping_a_task_cancels_reason_run_and_invalidates_its_lease(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    reason_run_id = completed["next_reason_run_id"]
    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=reason_run_id,
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}"

    stopped = client.post(f"/api/v1/tasks/{task['id']}/stop")

    assert stopped.status_code == 200, stopped.text
    run = client.get(f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}").json()
    assert run["status"] == "cancelled"
    assert run["worker"] is None
    assert run["lease_id"] is None
    assert client.get(f"/api/v1/reason-workspaces/{workspace['id']}").json()[
        "status"
    ] == "abandoned"
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["active_reason_run"] is None
    heartbeat = client.post(f"{base}/heartbeat", json={"lease_id": claim["lease_id"]})
    assert heartbeat.status_code == 409

    resumed = client.post(f"/api/v1/tasks/{task['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    assert reconciled["created_reason_run_ids"]
    assert reconciled["created_reason_run_ids"][0] != reason_run_id


def test_reason_run_can_read_project_credentials_only_while_its_lease_is_active(client) -> None:
    task, completed = _bootstrap_to_reason(client)
    credential = client.post(
        f"/api/v1/projects/{task['project_id']}/credentials",
        json={
            "name": "Reason 可读凭证",
            "content": "Bearer secret-token",
            "notes": "仅用于鉴权验证",
            "refresh_content": "使用 refresh-token 重新登录",
        },
    ).json()
    reason_run_id = completed["next_reason_run_id"]
    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=reason_run_id,
    )
    access = {
        "lease_id": claim["lease_id"],
        "reason_run_id": reason_run_id,
    }

    summaries = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/list",
        json=access,
    )
    material = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/{credential['id']}/get",
        json=access,
    )
    refresh = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credential-refreshes/"
        f"{credential['refresh_id']}/get",
        json=access,
    )

    assert summaries.status_code == 200, summaries.text
    assert summaries.json() == [{"id": credential["id"], "name": "Reason 可读凭证"}]
    assert material.status_code == 200, material.text
    assert material.json()["content"] == "Bearer secret-token"
    assert material.json()["refresh_id"] == credential["refresh_id"]
    assert refresh.status_code == 200, refresh.text
    assert refresh.json()["content"] == "使用 refresh-token 重新登录"

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    rejected = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/list",
        json=access,
    )
    assert rejected.status_code == 409


def test_reason_workspace_can_persist_more_proposals_than_dispatcher_quota(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        reason_target_open_intents=4,
        reason_max_new_intents_per_run=4,
    )
    claim, workspace = _active_workspace(client, task, completed["next_reason_run_id"])
    for number in range(1, 6):
        response = _add_todo(
            client,
            workspace,
            claim["lease_id"],
            _todo(number, completed["fact_id"]),
        )
        assert response.status_code == 200, response.text

    for number in range(1, 6):
        response = _propose(
            client,
            workspace,
            claim["lease_id"],
            _proposal(number, completed["fact_id"]),
        )
        assert response.status_code == 200, response.text
    status = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/status",
        json={"lease_id": claim["lease_id"]},
    ).json()
    assert status["todo_add_count"] == 5
    assert status["intent_proposal_count"] == 5
    # Runtime scheduling settings do not limit Reason's durable planning
    # backlog.  The only remaining bound is the workspace infrastructure cap.
    assert status["proposal_limit"] == 256
    assert status["remaining_intent_quota"] == 251
    # Workspace mutations remain private until the atomic commit.
    assert [
        item["id"] for item in client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"]
    ] == ["i001"]

    _assess_continue(client, workspace, claim["lease_id"], completed["fact_id"])
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["created_intent_ids"] == [
        "i002",
        "i003",
        "i004",
        "i005",
        "i006",
    ]


def test_reason_runtime_quota_does_not_bound_proposals_but_explore_budget_is_snapshotted(
    client,
) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        reason_batch_size=1,
        reason_min_interval_seconds=0,
        reason_target_open_intents=4,
        reason_max_new_intents_per_run=4,
        explore_execute_max_seconds=600,
    )
    assert len(_commit_plan(client, task, completed, count=3)) == 3
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f002",
                task_id=task["id"],
                description="新增待规划攻击面",
                created_at=utcnow(),
                archived=False,
            )
        )
    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    reason_run_id = reconciled["created_reason_run_ids"][0]
    claim, workspace = _active_workspace(client, task, reason_run_id)

    update_runtime_settings(
        client,
        reason_target_open_intents=8,
        reason_max_new_intents_per_run=8,
        explore_execute_max_seconds=3_600,
    )
    for number in (10, 11, 12):
        assert _add_todo(client, workspace, claim["lease_id"], _todo(number, "f002")).status_code == 200
    first = _propose(client, workspace, claim["lease_id"], _proposal(10, "f002"))
    second = _propose(client, workspace, claim["lease_id"], _proposal(11, "f002"))
    too_long = _proposal(12, "f002")
    too_long["execute_seconds"] = 1_200
    normalized_legacy_budget = _propose(client, workspace, claim["lease_id"], too_long)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert normalized_legacy_budget.status_code == 200, normalized_legacy_budget.text
    proposal = normalized_legacy_budget.json()["draft"]["intent_proposals"][-1]
    assert proposal["execute_seconds"] == 600
    assert proposal["conclude_seconds"] == 300


def test_reason_low_watermark_ignores_todos_that_already_have_open_explore(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        reason_batch_size=5,
        reason_low_watermark=2,
        reason_min_interval_seconds=0,
    )
    _commit_plan(client, task, completed, count=1)

    no_signal = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert no_signal.json()["created_reason_run_ids"] == []
    created_todo = client.post(
        f"/api/v1/tasks/{task['id']}/todos",
        json={
            "title": "尚未派发的可执行 Todo",
            "category": "recon",
            "sources": [{"type": "fact", "id": completed["fact_id"]}],
        },
    )
    assert created_todo.status_code == 201, created_todo.text

    triggered = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{triggered['created_reason_run_ids'][0]}"
    ).json()
    assert run["trigger"] == "low_watermark"
    assert created_todo.json()["id"] in run["trigger_payload"]["actionable_todo_ids"]


def test_unassessed_terminal_explore_reconciles_past_debounce_and_zero_low_watermark(
    client,
) -> None:
    task, intent_id = _closed_todo_terminal_explore_signal(client)

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    [reason_run_id] = reconciled.json()["created_reason_run_ids"]
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}"
    ).json()

    assert run["trigger"] == "intent_assessment"
    assert run["trigger_payload"]["unassessed_intent_ids"] == [intent_id]

    # The active run itself is the deduplication guard; a second dispatcher
    # sweep must not create another ReasonRun for the same assessment signal.
    repeated = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["created_reason_run_ids"] == []


def test_unassessed_terminal_explore_prefers_assessment_over_default_low_watermark(
    client,
) -> None:
    task, intent_id = _closed_todo_terminal_explore_signal(
        client,
        reason_low_watermark=4,
    )

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    [reason_run_id] = reconciled.json()["created_reason_run_ids"]
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}"
    ).json()

    assert run["trigger"] == "intent_assessment"
    assert run["trigger_payload"]["unassessed_intent_ids"] == [intent_id]


def test_intent_assessment_commits_once_and_rejects_a_later_reassessment(client) -> None:
    task, intent_id = _closed_todo_terminal_explore_signal(client)
    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    [reason_run_id] = reconciled["created_reason_run_ids"]
    claim, execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=reason_run_id,
    )
    assert execution["reason_prompt_profile"] == "progress"
    assert intent_id in execution["intent_description"]
    assert "评估本身不消费 Fact" in execution["intent_description"]
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    assessment = {
        "intent_id": intent_id,
        "status": "completed",
        "reason": "该 Intent 已完成验证，未发现需要继续扩展的方向",
    }

    staged = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intent-assessments",
        json={"lease_id": claim["lease_id"], "assessment": assessment},
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["draft"]["intent_assessments"] == [assessment]

    # Repeating the exact command while the Workspace is open is idempotent.
    repeated_stage = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intent-assessments",
        json={"lease_id": claim["lease_id"], "assessment": assessment},
    )
    assert repeated_stage.status_code == 200, repeated_stage.text
    assert len(repeated_stage.json()["draft"]["intent_assessments"]) == 1

    conflicting_stage = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/intent-assessments",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                **assessment,
                "status": "partial",
                "reason": "仍有部分边界需要继续确认",
            },
        },
    )
    assert conflicting_stage.status_code == 409

    goal = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/goal-assessment",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "目标仍需后续方向，但本轮没有新的 Intent",
                "evidence_refs": [],
            },
        },
    )
    assert goal.status_code == 200, goal.text
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text

    engine = client.app.state.container.engine
    with engine.connect() as conn:
        row = conn.execute(
            select(
                intents.c.assessment_status,
                intents.c.assessment_reason,
                intents.c.assessed_at,
            ).where(intents.c.task_id == task["id"], intents.c.id == intent_id)
        ).mappings().one()
    assert row["assessment_status"] == "completed"
    assert row["assessment_reason"] == assessment["reason"]
    assert row["assessed_at"] is not None

    # A later Reason run cannot assess the same terminal Intent a second time.
    manual = _trigger_manual_reason(client, task["id"], actor="重复评估回归")
    assert manual.status_code == 200, manual.text
    duplicate_claim, _ = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=manual.json()["id"],
    )
    duplicate_workspace = open_reason_workspace(
        client,
        task_id=task["id"],
        claim=duplicate_claim,
    )
    duplicate = client.post(
        f"/api/v1/reason-workspaces/{duplicate_workspace['id']}/intent-assessments",
        json={"lease_id": duplicate_claim["lease_id"], "assessment": assessment},
    )
    assert duplicate.status_code == 409
    assert "already been assessed" in duplicate.text


def test_reason_min_interval_and_max_wait_use_the_latest_runtime_config(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        reason_batch_size=2,
        reason_low_watermark=0,
        reason_max_wait_seconds=10,
        reason_min_interval_seconds=60,
    )
    _commit_plan(client, task, completed, count=1)
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f002",
                task_id=task["id"],
                description="等待时间已超过阈值的新 Fact",
                created_at="2020-01-01T00:00:00Z",
                archived=False,
            )
        )

    suppressed = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    assert suppressed["created_reason_run_ids"] == []
    update_runtime_settings(client, reason_min_interval_seconds=0)
    triggered = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{triggered['created_reason_run_ids'][0]}"
    ).json()
    assert run["trigger"] == "max_wait"
    assert run["source_fact_ids"] == ["f002"]


def test_terminal_reason_failure_does_not_recreate_the_same_input(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        execution_retry_limit=1,
        reason_min_interval_seconds=0,
    )
    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = (
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/"
        f"{completed['next_reason_run_id']}"
    )

    released = client.post(
        f"{base}/release",
        json={
            "lease_id": claim["lease_id"],
            "failure_kind": "invalid_reason_output",
            "failure_class": "execution",
        },
    )

    assert released.status_code == 200, released.text
    assert released.json()["status"] == "failed"
    with client.app.state.container.engine.connect() as conn:
        assert conn.execute(
            select(reason_workspaces.c.status).where(
                reason_workspaces.c.id == workspace["id"]
            )
        ).scalar_one() == "abandoned"
    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["created_reason_run_ids"] == []
    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "waiting"
    with client.app.state.container.engine.connect() as conn:
        assert conn.execute(
            select(func.count())
            .select_from(reason_runs)
            .where(reason_runs.c.task_id == task["id"])
        ).scalar_one() == 1


def test_failed_explore_and_resume_follow_trigger_rules(client) -> None:
    task, completed = _bootstrap_to_reason(
        client,
        execution_retry_limit=1,
        reason_low_watermark=2,
        reason_min_interval_seconds=0,
    )
    [intent_id] = _commit_plan(client, task, completed, count=1)

    stopped = client.post(f"/api/v1/tasks/{task['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    resumed = client.post(f"/api/v1/tasks/{task['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["active_reason_run"] is None
    no_unconditional_reason = client.post(
        "/api/v1/dispatch/reason-runs/reconcile",
        json={},
    )
    assert no_unconditional_reason.json()["created_reason_run_ids"] == []

    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}"
    assert client.post(f"{base}/start", json={"lease_id": claim["lease_id"]}).status_code == 200
    failed = client.post(
        f"{base}/release",
        json={
            "lease_id": claim["lease_id"],
            "failure_kind": "explore_contract_failed",
            "failure_class": "execution",
        },
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"

    triggered = client.post("/api/v1/dispatch/reason-runs/reconcile", json={}).json()
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{triggered['created_reason_run_ids'][0]}"
    ).json()
    # A failed terminal Explore must be assessed before the normal low-watermark
    # replenishment signal, otherwise its result can be forgotten.
    assert run["trigger"] == "intent_assessment"
    assert run["trigger_payload"]["failed_intent_ids"] == [intent_id]
    assert run["trigger_payload"]["unassessed_intent_ids"] == [intent_id]
