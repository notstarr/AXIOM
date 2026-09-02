from __future__ import annotations

from sqlalchemy import insert

from axiom.modules.graph.tables import facts
from axiom.shared.clock import utcnow
from protocol_helpers import claim_reason_run
from test_reason_run_control import (
    _bootstrap_to_reason,
    _finish_reason_run,
    _trigger_manual_reason,
)


def _claim_active(client, task: dict, reason_run_id: str) -> dict:
    claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=reason_run_id,
    )
    return claim


def test_active_reason_is_separate_from_fifo_pending_runs(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=3)
    active = _claim_active(client, task, completed["next_reason_run_id"])

    first = _trigger_manual_reason(client, task["id"], actor="Alice")
    assert first.status_code == 200, first.text
    hint_one = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "先验证 A", "creator": "Alice"},
    )
    hint_two = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "再验证 B", "creator": "Alice"},
    )
    assert hint_one.status_code == 201, hint_one.text
    assert hint_two.status_code == 201, hint_two.text

    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert current["active_reason_run"]["id"] == active["reason_run_id"]
    queued = current["queued_reason_runs"]
    assert [item["status"] for item in queued] == ["pending", "pending", "pending"]
    assert [item["trigger"] for item in queued] == ["manual", "hint", "hint"]
    assert [item["id"] for item in queued] == sorted(
        (item["id"] for item in queued),
        key=lambda value: int(value[2:]),
    )


def test_fact_trickle_below_batch_size_does_not_spawn_follow_up(client) -> None:
    """已有在跑/排队 ReasonRun 时，不足 reason_batch_size 的 fact 涓流
    不得再排新的 follow-up run（t_189 曾把 1+1 条 fact 拆成两个
    fact_batch run）。输入不丢：领取时 late-bind 搭车，或队列清空后
    由独立触发（批量阈值 / max_wait 兜底）消费。"""
    task, completed = _bootstrap_to_reason(
        client, reason_batch_size=4, reason_queue_limit=2
    )
    _claim_active(client, task, completed["next_reason_run_id"])
    queued = _trigger_manual_reason(client, task["id"], actor="Alice")
    assert queued.status_code == 200, queued.text

    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f-trickle",
                task_id=task["id"],
                description="不足批量的单条新 Fact",
                created_at=utcnow(),
                archived=False,
            )
        )

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["created_reason_run_ids"] == []

    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert [item["id"] for item in current["queued_reason_runs"]] == [
        queued.json()["id"]
    ]


def test_unassessed_intent_does_not_bypass_batch_gate(client) -> None:
    """终态未评估 Intent 不再是 follow-up 触发信号（t_189：每个 explore
    完成都伴随诞生一个 unassessed，把 fact_batch 闸门永久捅开，
    rr002323/rr002324 各带 1 条 fact 就成跑）。评估搭每班车：快照自带
    全量 unassessed，收口门禁要求清零，队列排空后由独立分支收尾。"""
    from axiom.modules.graph.tables import intents

    task, completed = _bootstrap_to_reason(
        client, reason_batch_size=4, reason_queue_limit=2
    )
    _claim_active(client, task, completed["next_reason_run_id"])
    queued = _trigger_manual_reason(client, task["id"], actor="Alice")
    assert queued.status_code == 200, queued.text

    now = utcnow()
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f-trickle-2",
                task_id=task["id"],
                description="伴随单条 fact 的评估信号",
                created_at=now,
                archived=False,
            )
        )
        conn.execute(
            insert(intents).values(
                id="i-unassessed",
                task_id=task["id"],
                kind="explore",
                description="已完成但未评估的探索",
                priority=50,
                status="completed",
                creator="worker-a",
                dedup_fingerprint="",
                concluded_at=now,
                created_at=now,
                archived=False,
            )
        )

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["created_reason_run_ids"] == []

    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert [item["id"] for item in current["queued_reason_runs"]] == [
        queued.json()["id"]
    ]


def test_queue_limit_coalesces_to_tail_and_preserves_trigger_metadata(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=1)
    _claim_active(client, task, completed["next_reason_run_id"])

    first = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "第一条提示", "creator": "Alice"},
    )
    second = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "第二条提示", "creator": "Bob"},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    [queued] = current["queued_reason_runs"]
    assert queued["trigger"] == "hint"
    assert queued["trigger_payload"]["coalesced_count"] == 1
    assert queued["trigger_payload"]["trigger_sources"] == ["hint"]
    assert [
        event["payload"]["hint_content"]
        for event in queued["trigger_payload"]["coalesced_triggers"]
    ] == ["第一条提示", "第二条提示"]


def test_same_fingerprint_reuses_matching_pending_not_queue_head(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=3)
    _claim_active(client, task, completed["next_reason_run_id"])

    first = _trigger_manual_reason(client, task["id"], actor="Alice")
    hint = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "不同输入", "creator": "Alice"},
    )
    duplicate = _trigger_manual_reason(client, task["id"], actor="Alice")
    assert first.status_code == 200, first.text
    assert hint.status_code == 201, hint.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == first.json()["id"]


def test_late_binding_merges_new_fact_without_losing_queued_snapshot(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=3)
    active_claim = _claim_active(client, task, completed["next_reason_run_id"])
    queued = _trigger_manual_reason(client, task["id"], actor="Alice")
    assert queued.status_code == 200, queued.text

    active_id = active_claim["reason_run_id"]
    _finish_reason_run(client.app.state.container.engine, task["id"], active_id)
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f002",
                task_id=task["id"],
                description="排队期间产生的新 Fact",
                created_at=utcnow(),
                archived=False,
            )
        )

    pending_id = queued.json()["id"]
    claim, execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=pending_id,
    )
    assert execution["reason_run_id"] == pending_id
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{pending_id}"
    ).json()
    assert "f002" in run["source_fact_ids"]
    assert run["trigger_payload"]["late_bound"] is True
    assert claim["reason_run_id"] == pending_id


def test_claim_enforces_one_in_flight_and_pending_fifo(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=3)
    active = _claim_active(client, task, completed["next_reason_run_id"])
    first = _trigger_manual_reason(client, task["id"], actor="first")
    second = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "second trigger", "creator": "second"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 201, second.text
    first_id = first.json()["id"]
    queued = client.get(f"/api/v1/tasks/{task['id']}").json()["queued_reason_runs"]
    assert [item["trigger"] for item in queued] == ["manual", "hint"]
    second_id = queued[1]["id"]

    active_conflict = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{first_id}/claim",
        json={"worker_name": "worker-a"},
    )
    assert active_conflict.status_code == 409, active_conflict.text

    _finish_reason_run(client.app.state.container.engine, task["id"], active["reason_run_id"])
    fifo_conflict = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{second_id}/claim",
        json={"worker_name": "worker-a"},
    )
    assert fifo_conflict.status_code == 409, fifo_conflict.text

    first_claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=first_id,
    )
    _finish_reason_run(client.app.state.container.engine, task["id"], first_id)
    second_claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=second_id,
    )
    assert first_claim["reason_run_id"] == first_id
    assert second_claim["reason_run_id"] == second_id


def test_terminal_reason_failure_releases_fifo_head_to_next_pending(client) -> None:
    task, completed = _bootstrap_to_reason(client, execution_retry_limit=1)
    active = _claim_active(client, task, completed["next_reason_run_id"])
    queued = _trigger_manual_reason(client, task["id"], actor="queued")
    assert queued.status_code == 200, queued.text

    released = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{active['reason_run_id']}/release",
        json={
            "lease_id": active["lease_id"],
            "failure_kind": "worker_failed",
            "failure_class": "execution",
        },
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "failed"

    next_claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=queued.json()["id"],
    )
    assert next_claim["reason_run_id"] == queued.json()["id"]


def test_stopping_task_cancels_active_and_all_pending_reason_runs(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=3)
    active = _claim_active(client, task, completed["next_reason_run_id"])
    first = _trigger_manual_reason(client, task["id"], actor="first")
    second = client.post(
        f"/api/v1/tasks/{task['id']}/hints",
        json={"content": "排队中的提示", "creator": "second"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 201, second.text

    stopped = client.post(f"/api/v1/tasks/{task['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert current["active_reason_run"] is None
    assert current["queued_reason_runs"] == []

    for run_id in (active["reason_run_id"], first.json()["id"]):
        run = client.get(f"/api/v1/tasks/{task['id']}/reason-runs/{run_id}").json()
        assert run["status"] == "cancelled"
