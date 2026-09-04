from __future__ import annotations

from sqlalchemy import insert, text

from axiom.modules.graph.tables import intents
from axiom.shared.clock import utcnow
from protocol_helpers import (
    claim_reason_run,
    open_reason_workspace,
    update_runtime_settings,
)
from test_reason_workspace import active_reason


def _release_with_plan_invalid(client, task_id: str, claim: dict) -> dict:
    response = client.post(
        f"/api/v1/dispatch/tasks/{task_id}/reason-runs/{claim['reason_run_id']}/release",
        json={
            "lease_id": claim["lease_id"],
            "failure_kind": "reason_plan_invalid",
            "failure_class": "execution",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_draft_survives_retry_after_plan_invalid(client) -> None:
    update_runtime_settings(client, retry_base_seconds=1)
    task, claim, completed = active_reason(client)
    run_id = claim["reason_run_id"]

    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    fact_id = completed["fact_id"]
    staged = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/todos",
        json={
            "lease_id": claim["lease_id"],
            "todo": {
                "ref": "td-continuity",
                "title": "延续性验证方向",
                "category": "authorization",
                "sources": [{"type": "fact", "id": fact_id}],
            },
        },
    )
    assert staged.status_code == 200, staged.text

    released = _release_with_plan_invalid(client, task["id"], claim)
    assert released["status"] == "pending"
    import time; time.sleep(1.3)

    claim2, _execution = claim_reason_run(
        client, task_id=task["id"], reason_run_id=run_id
    )
    assert claim2["lease_id"] != claim["lease_id"]

    reopened = open_reason_workspace(client, task_id=task["id"], claim=claim2)
    assert reopened["id"] == workspace["id"], "重试必须沿用上次的 workspace"
    assert reopened["draft"]["todo_adds"], "草稿内容必须保留"
    assert reopened["draft"]["todo_adds"][0]["ref"] == "td-continuity"

    with client.app.state.container.engine.connect() as conn:
        readopted = conn.execute(
            text(
                "SELECT count(*) FROM axiom.audit_events "
                "WHERE task_id=:t AND event_type='reason.workspace_readopted'"
            ),
            {"t": task["id"]},
        ).scalar_one()
    assert readopted >= 1, "应记录 workspace 续用审计事件"


def _seed_terminal_intent(client, task_id: str, intent_id: str) -> None:
    now = utcnow()
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            insert(intents).values(
                id=intent_id,
                task_id=task_id,
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


def _assess(client, task: dict, claim: dict, workspace_id: str, intent_id: str, reason: str):
    return client.post(
        f"/api/v1/reason-workspaces/{workspace_id}/intent-assessments",
        json={
            "lease_id": claim["lease_id"],
            "assessment": {
                "intent_id": intent_id,
                "status": "completed",
                "reason": reason,
            },
        },
    )


def test_assessment_resubmission_replaces_stale_draft_entry(client) -> None:
    update_runtime_settings(client, retry_base_seconds=1)
    task, claim, completed = active_reason(client)
    run_id = claim["reason_run_id"]
    _seed_terminal_intent(client, task["id"], "i-stale")

    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    first = _assess(
        client, task, claim, workspace["id"], "i-stale", "第一轮结论"
    )
    assert first.status_code == 200, first.text

    released = _release_with_plan_invalid(client, task["id"], claim)
    assert released["status"] == "pending"
    import time; time.sleep(1.3)
    claim2, _execution = claim_reason_run(
        client, task_id=task["id"], reason_run_id=run_id
    )
    reopened = open_reason_workspace(client, task_id=task["id"], claim=claim2)
    assert reopened["id"] == workspace["id"]

    resubmitted_same = _assess(
        client, task, claim2, reopened["id"], "i-stale", "第一轮结论"
    )
    assert resubmitted_same.status_code == 200, resubmitted_same.text

    resubmitted_new = _assess(
        client, task, claim2, reopened["id"], "i-stale", "重放后的最新结论"
    )
    assert resubmitted_new.status_code == 200, resubmitted_new.text
    entries = [
        item
        for item in resubmitted_new.json()["draft"]["intent_assessments"]
        if item["intent_id"] == "i-stale"
    ]
    assert len(entries) == 1
    assert entries[0]["reason"] == "重放后的最新结论"


def test_assessment_of_already_assessed_intent_is_noop(client) -> None:
    update_runtime_settings(client, retry_base_seconds=1)
    task, claim, completed = active_reason(client)
    _seed_terminal_intent(client, task["id"], "i-done")
    now = utcnow()
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            intents.update()
            .where(intents.c.task_id == task["id"], intents.c.id == "i-done")
            .values(
                assessment_status="partial",
                assessment_reason="上一班已评估",
                assessed_at=now,
            )
        )

    workspace = open_reason_workspace(client, task_id=task["id"], claim=claim)
    response = _assess(
        client, task, claim, workspace["id"], "i-done", "重复评估应为 no-op"
    )
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["intent_assessments"] == []
