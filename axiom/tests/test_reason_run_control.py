from __future__ import annotations

from sqlalchemy import func, insert, select, update

from axiom.modules.graph.tables import facts
from axiom.modules.reason.tables import reason_runs, reason_workspaces
from axiom.shared.clock import utcnow
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
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 8},
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
        "description": f"验证接口方向 {number} 的授权边界",
        "targets": ["a.example.com"],
        "execute_seconds": 300,
        "conclude_seconds": 60,
    }


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
            "credential_type": "bearer",
            "target_host": "a.example.com",
            "data": {"token": "secret-token"},
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

    assert summaries.status_code == 200, summaries.text
    assert summaries.json()[0]["data_fields"] == ["token"]
    assert material.status_code == 200, material.text
    assert material.json()["data"]["token"] == "secret-token"

    assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200
    rejected = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/list",
        json=access,
    )
    assert rejected.status_code == 409


def test_reason_workspace_todos_can_outnumber_intents_and_fifth_proposal_is_rejected(client) -> None:
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

    for number in range(1, 5):
        response = _propose(
            client,
            workspace,
            claim["lease_id"],
            _proposal(number, completed["fact_id"]),
        )
        assert response.status_code == 200, response.text
    fifth = _propose(
        client,
        workspace,
        claim["lease_id"],
        _proposal(5, completed["fact_id"]),
    )

    assert fifth.status_code == 409
    assert "Intent quota exhausted" in fifth.json()["error"]["message"]
    status = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/status",
        json={"lease_id": claim["lease_id"]},
    ).json()
    assert status["todo_add_count"] == 5
    assert status["intent_proposal_count"] == 4
    assert status["remaining_intent_quota"] == 0
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
    assert committed.json()["created_intent_ids"] == ["i002", "i003", "i004", "i005"]


def test_reason_quota_and_explore_budget_bounds_are_snapshotted_at_claim(client) -> None:
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
    out_of_snapshot_bounds = _propose(client, workspace, claim["lease_id"], too_long)

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert "open=3, target=4" in second.json()["error"]["message"]
    assert out_of_snapshot_bounds.status_code == 409
    assert "60-600" in out_of_snapshot_bounds.json()["error"]["message"]


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
    assert run["trigger"] == "low_watermark"
    assert run["trigger_payload"]["failed_intent_ids"] == [intent_id]
