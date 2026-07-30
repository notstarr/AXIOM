from __future__ import annotations

from sqlalchemy import select

from axiom.modules.graph.tables import intents
from protocol_helpers import claim_reason_run, open_reason_workspace


def _completed_task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Continuation"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Continuation",
            "origin": "a.example.com",
            "goal": "完成第一轮目标",
            "worker_pool": ["worker-a"],
        },
    ).json()
    completed = client.post(
        f"/api/v1/tasks/{task['id']}/goal-evaluations",
        json={
            "achieved": True,
            "reason": "第一轮目标已达到",
            "evidence_refs": ["fact:origin"],
            "evaluator": "human",
        },
    )
    assert completed.status_code == 201, completed.text
    return client.get(f"/api/v1/tasks/{task['id']}").json()


def _continue(client, task_id: str, goal: str = "第二轮唯一目标") -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/continuations",
        json={
            "goal": goal,
            "reason": "第一轮结果不够深入",
            "focus": "优先复盘授权边界",
            "run_duration_seconds": 3600,
            "actor": "human.test",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_completed_task_continues_with_new_goal_and_reason_run(client) -> None:
    task = _completed_task(client)
    first_cycle_id = task["current_goal_cycle_id"]

    continued = _continue(client, task["id"])
    current = continued["task"]
    assert current["id"] == task["id"]
    assert current["status"] == "running"
    assert current["phase"] == "reason"
    assert current["goal"] == "第二轮唯一目标"
    assert current["completed_at"] is None
    assert current["current_goal_cycle_id"] != first_cycle_id
    assert continued["goal_cycle"]["sequence"] == 2
    assert continued["goal_cycle"]["continuation_reason"] == "第一轮结果不够深入"

    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{continued['reason_run_id']}"
    ).json()
    assert run["trigger"] == "continuation"
    assert run["goal_cycle_id"] == current["current_goal_cycle_id"]
    assert run["source_fact_ids"] == []
    assert run["trigger_payload"]["previous_goal"] == "完成第一轮目标"
    assert run["trigger_payload"]["current_goal"] == "第二轮唯一目标"

    cycles = client.get(f"/api/v1/tasks/{task['id']}/goal-cycles").json()
    assert [cycle["sequence"] for cycle in cycles] == [2, 1]
    assert cycles[1]["status"] == "completed"
    assert cycles[0]["status"] == "active"
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert len(graph["intents"]) == 1
    assert graph["goal_from_fact_ids"] == []
    assert graph["goal_history"][0]["goal_cycle_id"] == first_cycle_id


def test_continuation_reason_can_finish_without_new_intents(client) -> None:
    task = _completed_task(client)
    continued = _continue(client, task["id"], goal="复核第一轮目标")
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["reason"], "max_running": 1},
    )
    claim, execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=continued["reason_run_id"],
    )
    assert "当前唯一 Goal：复核第一轮目标" in execution["intent_description"]
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = f"/api/v1/reason-workspaces/{workspace['id']}"
    assessed = client.post(
        f"{base}/goal-assessment",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "achieved": True,
                "reason": "历史证据仍足以证明当前目标",
                "evidence_refs": ["fact:origin"],
            },
        },
    )
    assert assessed.status_code == 200, assessed.text
    committed = client.post(f"{base}/commit", json={"lease_id": claim["lease_id"]})
    assert committed.status_code == 200, committed.text
    assert committed.json()["created_intent_ids"] == []

    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert current["status"] == "completed"
    cycles = client.get(f"/api/v1/tasks/{task['id']}/goal-cycles").json()
    assert cycles[0]["status"] == "completed"
    evaluation = client.get(f"/api/v1/tasks/{task['id']}/goal-evaluations").json()[0]
    assert evaluation["goal_cycle_id"] == cycles[0]["id"]


def test_continuation_rejects_non_completed_and_concurrent_requests(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Running"}).json()
    running = client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "Running", "origin": "a.example.com"},
    ).json()
    rejected = client.post(
        f"/api/v1/tasks/{running['id']}/continuations",
        json={"goal": "新目标", "reason": "继续"},
    )
    assert rejected.status_code == 409

    completed = _completed_task(client)
    _continue(client, completed["id"])
    repeated = client.post(
        f"/api/v1/tasks/{completed['id']}/continuations",
        json={"goal": "第三轮目标", "reason": "重复请求"},
    )
    assert repeated.status_code == 409


def test_continuation_reason_creates_intents_in_current_goal_cycle(client, engine) -> None:
    task = _completed_task(client)
    continued = _continue(client, task["id"])
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["reason"], "max_running": 1},
    )
    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=continued["reason_run_id"],
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = f"/api/v1/reason-workspaces/{workspace['id']}"
    todo = client.post(
        f"{base}/todos",
        json={
            "lease_id": claim["lease_id"],
            "todo": {
                "ref": "authz",
                "title": "复核对象级授权",
                "category": "authorization",
                "sources": [{"type": "fact", "id": "origin"}],
            },
        },
    )
    assert todo.status_code == 200, todo.text
    proposal = client.post(
        f"{base}/intents",
        json={
            "lease_id": claim["lease_id"],
            "intent": {
                "todo_ref": "authz",
                "from_facts": ["origin"],
                "description": "验证对象级授权是否存在跨主体读取",
                "targets": ["a.example.com"],
                "execute_seconds": 300,
                "conclude_seconds": 180,
            },
        },
    )
    assert proposal.status_code == 200, proposal.text
    assessed = client.post(
        f"{base}/goal-assessment",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "仍需执行新的授权验证",
                "evidence_refs": ["fact:origin"],
            },
        },
    )
    assert assessed.status_code == 200, assessed.text
    committed = client.post(f"{base}/commit", json={"lease_id": claim["lease_id"]})
    assert committed.status_code == 200, committed.text
    [intent_id] = committed.json()["created_intent_ids"]
    with engine.connect() as conn:
        cycle_id = conn.execute(
            select(intents.c.goal_cycle_id).where(
                intents.c.task_id == task["id"], intents.c.id == intent_id
            )
        ).scalar_one()
    assert cycle_id == continued["goal_cycle"]["id"]


def test_zero_intent_continuation_does_not_auto_retrigger_on_old_goal_cancellations(client) -> None:
    task = _completed_task(client)
    continued = _continue(client, task["id"])
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["reason"], "max_running": 1},
    )
    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=continued["reason_run_id"],
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    base = f"/api/v1/reason-workspaces/{workspace['id']}"
    assessed = client.post(
        f"{base}/goal-assessment",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "当前没有材料性新增方向，等待人工补充",
                "evidence_refs": ["fact:origin"],
            },
        },
    )
    assert assessed.status_code == 200, assessed.text
    committed = client.post(f"{base}/commit", json={"lease_id": claim["lease_id"]})
    assert committed.status_code == 200, committed.text
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "waiting"

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile").json()
    assert reconciled["created_reason_run_ids"] == []
