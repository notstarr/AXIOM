from __future__ import annotations

import json

from sqlalchemy import insert, select, update

from axiom.modules.graph.tables import facts, intents
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import utcnow
from protocol_helpers import complete_bootstrap, update_runtime_settings


def _task_with_completed_bootstrap(client, engine) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Plan lifecycle"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Plan lifecycle",
            "origin": "a.example.com",
            "goal": "验证 Plan 生命周期",
            "worker_pool": ["plan-worker"],
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/plan-worker",
        json={"capabilities": ["bootstrap", "reason", "plan"], "max_running": 2},
    )
    assert worker.status_code == 200, worker.text
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "plan-worker"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": claim["lease_id"]}).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=claim["lease_id"],
        files={
            "asset_inventory.md": "# Assets\n- a.example.com",
            "initial_leads.md": "# Leads\n- review routes",
            "findings.md": "# Findings\n- no confirmed issue",
        },
    )
    now = completed["artifacts"][0]["created_at"]
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.id == completed["next_reason_run_id"],
            )
            .values(status="completed", concluded_at=now, updated_at=now)
        )
        conn.execute(
            update(tasks).where(tasks.c.id == task["id"]).values(phase="waiting")
        )
    return task


def _claim_plan(client, task_id: str, plan_run_id: str) -> dict:
    base = f"/api/v1/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "plan-worker"})
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    lease = {"lease_id": claimed["lease_id"]}
    started = client.post(f"{base}/start", json=lease)
    assert started.status_code == 200, started.text
    execution = client.post(f"{base}/input", json=lease)
    assert execution.status_code == 200, execution.text
    claimed["execution"] = execution.json()
    return claimed


def _claim_plan_only(client, task_id: str, plan_run_id: str) -> dict:
    base = f"/api/v1/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "plan-worker"})
    assert claim.status_code == 200, claim.text
    return claim.json()


def test_pending_independent_plan_is_cancelled_when_new_fact_arrives(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    plan = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert plan.status_code == 200, plan.text
    plan_run_id = plan.json()["id"]

    with engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f-lifecycle-new",
                task_id=task["id"],
                description="new input after Plan was queued",
                created_at=utcnow(),
                archived=False,
            )
        )
    update_runtime_settings(client, reason_min_interval_seconds=0)
    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text

    stored = client.get(
        f"/api/v1/tasks/{task['id']}/plan-runs/{plan_run_id}"
    ).json()
    assert stored["status"] == "cancelled"
    assert stored["last_failure_kind"] == "incremental_work_arrived"
    assert reconciled.json()["created_reason_run_ids"]


def test_failed_plan_handoff_is_not_recreated_forever(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    plan = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    ).json()
    claimed = _claim_plan(client, task["id"], plan["id"])
    lease = {"lease_id": claimed["lease_id"]}
    execution = claimed["execution"]
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": plan["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    ).json()
    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/decision",
        json={**lease, "decision": {"decision": "needs_reason"}},
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/commit", json=lease
    )
    assert committed.status_code == 200, committed.text

    first = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert first.status_code == 200, first.text
    [handoff_id] = first.json()["created_reason_run_ids"]
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == handoff_id)
            .values(
                status="failed",
                failure_class="execution",
                last_failure_kind="worker_failed",
                concluded_at=now,
                updated_at=now,
            )
        )

    second = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert second.status_code == 200, second.text
    assert second.json()["created_reason_run_ids"] == []

    with engine.connect() as conn:
        handoffs = conn.execute(
            select(reason_runs.c.id)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.trigger == "plan_handoff",
            )
        ).scalars().all()
    assert handoffs == [handoff_id]


def test_plan_commit_preserves_existing_pending_reason_work(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    plan = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    ).json()
    claimed = _claim_plan(client, task["id"], plan["id"])
    lease = {"lease_id": claimed["lease_id"]}
    execution = claimed["execution"]
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": plan["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    ).json()
    reason = client.post(
        f"/api/v1/tasks/{task['id']}/reason-runs/trigger",
        json={"actor": "arrived-during-plan"},
    )
    assert reason.status_code == 200, reason.text

    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/decision",
        json={
            **lease,
            "decision": {
                "decision": "exhausted",
                "reviewed_dimensions": ["coverage"],
                "exhaustion_reason": "no additional global signal",
                "confidence": "high",
            },
        },
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/commit", json=lease
    )
    assert committed.status_code == 200, committed.text
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"


def test_plan_start_rechecks_explore_owner_after_claim(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    plan = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    ).json()
    claimed = _claim_plan_only(client, task["id"], plan["id"])

    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(intents).values(
                id="i-lifecycle-explore",
                task_id=task["id"],
                goal_cycle_id=None,
                todo_id=None,
                kind="explore",
                description="Explore arrived after Plan claim",
                priority=50,
                status="pending",
                creator="test",
                worker=None,
                worker_pool_json=json.dumps(["plan-worker"]),
                targets_json=json.dumps(["a.example.com"]),
                dedup_fingerprint="",
                plan_metadata_json="{}",
                execute_timeout_seconds=None,
                conclude_timeout_seconds=None,
                lease_id=None,
                claimed_at=None,
                last_heartbeat_at=None,
                retry_count=0,
                infrastructure_retry_count=0,
                next_retry_at=None,
                last_failure_kind="",
                failure_class="",
                activity_kind="pending",
                activity_summary="",
                activity_tool="",
                activity_started_at=None,
                activity_updated_at=now,
                to_fact_id=None,
                human_intervention_required=False,
                human_intervention_reason="",
                human_intervention_marked_at=None,
                assessment_status="open",
                assessment_reason="",
                assessed_at=None,
                created_at=now,
                concluded_at=None,
                archived=False,
            )
        )

    base = f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan['id']}"
    started = client.post(f"{base}/start", json={"lease_id": claimed["lease_id"]})
    assert started.status_code == 409, started.text
    assert "Explore" in started.text or "Bootstrap" in started.text
