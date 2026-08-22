from __future__ import annotations

import pytest
from sqlalchemy import select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.tables import facts, intents
from axiom.modules.plan.tables import plan_runs
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import tasks
from axiom.modules.todos.tables import todos
from axiom.modules.vulnerabilities.tables import vulnerabilities
from axiom.shared.clock import utcnow
from protocol_helpers import complete_bootstrap


def _task_with_completed_bootstrap(client, engine) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Plan ownership"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Plan ownership",
            "origin": "ownership.example.com",
            "goal": "Verify cross-stage Fact ownership",
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
    )
    assert claim.status_code == 200, claim.text
    lease_id = claim.json()["lease_id"]
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    started = client.post(f"{base}/start", json={"lease_id": lease_id})
    assert started.status_code == 200, started.text
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=lease_id,
        files={
            "asset_inventory.md": "# Assets\n- ownership.example.com",
            "initial_leads.md": "# Leads\n- verify ownership",
            "findings.md": "# Findings\n- no confirmed issue",
        },
    )
    # Keep the fixture at the handoff boundary.  The initial Reason run is
    # already consumed by Bootstrap and must not remain in the way of Plan.
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.id == completed["next_reason_run_id"],
            )
            .values(
                status="completed",
                concluded_at=completed["artifacts"][0]["created_at"],
                updated_at=completed["artifacts"][0]["created_at"],
            )
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(phase="waiting")
        )
    return task


def _claim_plan(client, task_id: str, plan_run_id: str) -> tuple[dict, dict]:
    base = f"/api/v1/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "plan-worker"})
    assert claim.status_code == 200, claim.text
    claimed = claim.json()
    lease = {"lease_id": claimed["lease_id"]}
    started = client.post(f"{base}/start", json=lease)
    assert started.status_code == 200, started.text
    execution = client.post(f"{base}/input", json=lease)
    assert execution.status_code == 200, execution.text
    return claimed, execution.json()


def _open_plan_workspace(client, task_id: str, run: dict, claimed: dict, execution: dict) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _insert_fact(engine, task_id: str, fact_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            facts.insert().values(
                id=fact_id,
                task_id=task_id,
                description=f"fresh Fact {fact_id}",
                created_at=utcnow(),
                archived=False,
            )
        )


def test_completed_plan_fact_is_not_reenqueued_by_reason(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    fact_id = "f-plan-owned"
    _insert_fact(engine, task["id"], fact_id)

    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "ownership"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    assert fact_id in run["source_fact_ids"]
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = _open_plan_workspace(client, task["id"], run, claimed, execution)

    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/decision",
        json={"lease_id": claimed["lease_id"], "decision": {"decision": "needs_reason"}},
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/commit",
        json={"lease_id": claimed["lease_id"]},
    )
    assert committed.status_code == 200, committed.text

    plan_after = client.get(f"/api/v1/tasks/{task['id']}/plan-runs/{run['id']}")
    assert plan_after.status_code == 200, plan_after.text
    assert plan_after.json()["status"] == "completed"

    reason = client.post(
        f"/api/v1/tasks/{task['id']}/reason-runs/trigger",
        json={"actor": "reason-check"},
    )
    assert reason.status_code == 200, reason.text
    assert fact_id not in reason.json()["source_fact_ids"]

    with engine.connect() as conn:
        owned = conn.execute(
            select(plan_runs.c.status)
            .select_from(plan_runs)
            .where(plan_runs.c.task_id == task["id"], plan_runs.c.id == run["id"])
        ).scalar_one()
    assert owned == "completed"


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
@pytest.mark.parametrize("consumer", ["reason", "plan"])
def test_failed_or_cancelled_plan_fact_can_be_reconsumed(
    client, engine, terminal_status: str, consumer: str
) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    fact_id = f"f-retry-{terminal_status}-{consumer}"
    _insert_fact(engine, task["id"], fact_id)

    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "ownership"},
    )
    assert triggered.status_code == 200, triggered.text
    old_run = triggered.json()
    assert fact_id in old_run["source_fact_ids"]
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(plan_runs)
            .where(plan_runs.c.task_id == task["id"], plan_runs.c.id == old_run["id"])
            .values(
                status=terminal_status,
                worker=None,
                lease_id=None,
                concluded_at=now,
                updated_at=now,
                last_failure_kind=("test_failed" if terminal_status == "failed" else "test_cancelled"),
            )
        )

    if consumer == "reason":
        response = client.post(
            f"/api/v1/tasks/{task['id']}/reason-runs/trigger",
            json={"actor": "reason-retry"},
        )
        assert response.status_code == 200, response.text
        assert fact_id in response.json()["source_fact_ids"]
    else:
        response = client.post(
            f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
            json={"actor": "plan-retry", "trigger": "retry"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] != old_run["id"]
        assert fact_id in response.json()["source_fact_ids"]


@pytest.mark.parametrize(
    ("root_type", "root_id", "with_todo_lineage"),
    [
        ("fact", "f-archive-plan", False),
        # Todo is covered as a member of a Fact -> Intent closure.  Todo is
        # intentionally not an Archive API root; its direct lifecycle is the
        # obsolete soft-delete protocol owned by Plan/Reason workspaces.
        ("fact", "f-archive-plan-todo-output", True),
        ("artifact", "a-archive-plan", False),
        ("vulnerability", "v-archive-plan", False),
    ],
)
def test_archive_invalidates_active_plan_and_workspace(
    client, engine, root_type: str, root_id: str, with_todo_lineage: bool
) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    now = utcnow()
    with engine.begin() as conn:
        if root_type == "fact":
            conn.execute(
                facts.insert().values(
                    id=root_id,
                    task_id=task["id"],
                    description="archive Plan context Fact",
                    created_at=now,
                    archived=False,
                )
            )
            if with_todo_lineage:
                conn.execute(
                    todos.insert().values(
                        id="td-archive-plan",
                        task_id=task["id"],
                        title="Todo in archive closure",
                        category="recon",
                        theme="archive",
                        priority=50,
                        status="completed",
                        created_by="test",
                        status_reason="covered by closure",
                        created_at=now,
                        updated_at=now,
                        completed_at=now,
                        archived=False,
                    )
                )
                conn.execute(
                    intents.insert().values(
                        id="i-archive-plan",
                        task_id=task["id"],
                        goal_cycle_id=None,
                        todo_id="td-archive-plan",
                        kind="explore",
                        description="Fact closure producer",
                        priority=50,
                        status="completed",
                        creator="test",
                        worker=None,
                        worker_pool_json="[]",
                        targets_json="[]",
                        dedup_fingerprint="",
                        plan_metadata_json="{}",
                        execute_timeout_seconds=300,
                        conclude_timeout_seconds=180,
                        lease_id=None,
                        claimed_at=None,
                        last_heartbeat_at=None,
                        retry_count=0,
                        infrastructure_retry_count=0,
                        next_retry_at=None,
                        last_failure_kind="",
                        failure_class="",
                        activity_kind="",
                        activity_summary="",
                        activity_tool="",
                        activity_started_at=None,
                        activity_updated_at=None,
                        to_fact_id=root_id,
                        human_intervention_required=False,
                        human_intervention_reason="",
                        human_intervention_marked_at=None,
                        assessment_status="completed",
                        assessment_reason="covered by closure",
                        assessed_at=now,
                        created_at=now,
                        concluded_at=now,
                        archived=False,
                    )
                )
        elif root_type == "artifact":
            conn.execute(
                artifacts.insert().values(
                    id=root_id,
                    task_id=task["id"],
                    intent_id=None,
                    review_run_id=None,
                    artifact_type="plan.archive",
                    storage_key=f"tasks/{task['id']}/artifacts/{root_id}.md",
                    summary="archive Plan context artifact",
                    content_hash="archive-plan",
                    size=1,
                    created_by="test",
                    created_at=now,
                    archived=False,
                )
            )
        elif root_type == "vulnerability":
            conn.execute(
                vulnerabilities.insert().values(
                    id=root_id,
                    task_id=task["id"],
                    intent_id=None,
                    title="archive Plan context vulnerability",
                    type="test",
                    severity="low",
                    description="archive Plan context vulnerability",
                    prerequisites="",
                    impact="none",
                    reproduction_steps_json="[]",
                    remediation="",
                    curl_command="",
                    evidence_json="[]",
                    affected_target="ownership.example.com",
                    status="待验证",
                    status_reason="",
                    source="test",
                    reported_by="test",
                    created_at=now,
                    updated_at=now,
                    archived=False,
                    format_version=2,
                )
            )
        else:
            conn.execute(
                todos.insert().values(
                    id=root_id,
                    task_id=task["id"],
                    title="archive Plan context Todo",
                    category="recon",
                    theme="archive",
                    priority=50,
                    status="open",
                    created_by="test",
                    status_reason="",
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                    archived=False,
                )
            )

    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "archive"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = _open_plan_workspace(client, task["id"], run, claimed, execution)

    preview = client.post(
        f"/api/v1/tasks/{task['id']}/archives/preview",
        json={"root_type": root_type, "root_id": root_id},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["blockers"] == []
    if with_todo_lineage:
        members = {
            (item["entity_type"], item["entity_id"])
            for item in preview.json()["members"]
        }
        assert ("todo", "td-archive-plan") in members
    archived = client.post(
        f"/api/v1/tasks/{task['id']}/archives",
        json={
            "root_type": root_type,
            "root_id": root_id,
            "expected_closure_hash": preview.json()["closure_hash"],
            "reason": "invalidate Plan context",
            "actor": "test",
        },
    )
    assert archived.status_code == 201, archived.text

    with engine.connect() as conn:
        run_row = conn.execute(
            select(plan_runs.c.status, plan_runs.c.last_failure_kind).where(
                plan_runs.c.task_id == task["id"], plan_runs.c.id == run["id"]
            )
        ).mappings().one()
    workspace_after = client.get(f"/api/v1/plan-workspaces/{workspace['id']}")
    assert workspace_after.status_code == 200, workspace_after.text
    assert run_row["status"] == "cancelled"
    assert run_row["last_failure_kind"] == "archive_context_invalidated"
    assert workspace_after.json()["status"] == "abandoned"
    assert workspace_after.json()["failure_reason"] == "archive_context_invalidated"
