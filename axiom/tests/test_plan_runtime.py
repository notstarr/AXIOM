from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import insert, select, update

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.graph.tables import facts, intents
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.plan.tables import plan_runs
from axiom.modules.reason.tables import reason_runs
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todos
from axiom.shared.clock import utcnow
from protocol_helpers import complete_bootstrap
from protocol_helpers import update_runtime_settings


def _task_with_completed_bootstrap(client, engine) -> dict:
    project = client.post("/api/v1/projects", json={"title": "独立 Plan"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Plan runtime",
            "origin": "a.example.com",
            "goal": "覆盖全局攻击面",
            "worker_pool": ["plan-worker"],
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/plan-worker",
        json={"capabilities": ["bootstrap", "plan"], "max_running": 1},
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
            "initial_leads.md": "# Leads\n- review org routes",
            "findings.md": "# Findings\n- no confirmed issue",
        },
    )
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.id == completed["next_reason_run_id"],
            )
            .values(status="completed", concluded_at=completed["artifacts"][0]["created_at"], updated_at=completed["artifacts"][0]["created_at"])
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


def test_plan_worker_pool_isolated_from_reason_pool_with_legacy_fallback(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "Plan pool"}).json()
    task_response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Plan pool isolation",
            "origin": "a.example.com",
            "goal": "验证独立 Plan 后端池",
            "worker_pool": ["reason-worker"],
            "plan_worker_pool": ["plan-worker"],
        },
    )
    assert task_response.status_code == 201, task_response.text
    task = task_response.json()
    assert task["worker_pool"] == ["reason-worker"]
    assert task["plan_worker_pool"] == ["plan-worker"]

    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    assert triggered.json()["worker_pool"] == ["plan-worker"]

    # Clearing the optional Plan pool restores compatibility inheritance: a
    # subsequent Plan snapshot follows the current ordinary task pool.
    updated = client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"plan_worker_pool": [], "worker_pool": ["reason-worker-2"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["plan_worker_pool"] == []
    assert updated.json()["worker_pool"] == ["reason-worker-2"]

    inherited = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual-2"},
    )
    assert inherited.status_code == 200, inherited.text
    # The first pending Plan is coalesced only when its fingerprint matches;
    # the new pool is still the source of truth for a newly created row.
    assert inherited.json()["worker_pool"] == ["reason-worker-2"]


def test_independent_plan_run_workspace_commit_dispatches_explore(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    assert run["id"].startswith("pr")
    assert run["status"] == "pending"

    claimed, execution = _claim_plan(client, task["id"], run["id"])
    assert execution["work_kind"] == "plan"
    assert execution["phase"] == "plan"
    assert execution["prompt_profile"] == "plan"
    assert execution["plan_run_id"] == run["id"]
    assert execution["context_manifest_path"]

    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    workspace_id = workspace.json()["id"]
    lease = {"lease_id": claimed["lease_id"]}

    todo = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/todos",
        json={
            **lease,
            "todo": {
                "ref": "org-routes",
                "title": "Review organization routes",
                "category": "authorization",
                "sources": [{"type": "fact", "id": "f001"}],
            },
        },
    )
    assert todo.status_code == 200, todo.text
    intent = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/intents",
        json={
            **lease,
            "intent": {
                "todo_ref": "org-routes",
                "from_facts": ["f001"],
                "source_refs": ["fact:f001"],
                "description": "Review organization routes for authorization gaps",
                "targets": ["a.example.com"],
                "endpoint": "/api/v1/orgs/{orgId}",
                "test_vector": "cross-tenant object access",
                "why_now": "Plan found an uncovered organization capability tree",
                "expected_signal": "another tenant object is readable",
                "stop_condition": "stop after the route family is covered",
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
                "reviewed_dimensions": ["coverage", "weaponization"],
                "confidence": "medium",
            },
        },
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(f"/api/v1/plan-workspaces/{workspace_id}/commit", json=lease)
    assert committed.status_code == 200, committed.text
    payload = committed.json()
    assert len(payload["created_todo_ids"]) == 1
    assert len(payload["created_intent_ids"]) == 1
    with engine.connect() as conn:
        intent_row = conn.execute(
            select(intents.c.dedup_fingerprint, intents.c.plan_metadata_json).where(
                intents.c.task_id == task["id"],
                intents.c.id == payload["created_intent_ids"][0],
            )
        ).mappings().one()
    assert intent_row["dedup_fingerprint"]
    metadata = json.loads(intent_row["plan_metadata_json"] or "{}")
    assert metadata["plan_run_id"] == run["id"]
    assert metadata["plan_workspace_id"] == workspace_id
    assert metadata["dedup_fingerprint"] == intent_row["dedup_fingerprint"]
    assert metadata["source_refs"] == ["fact:f001"]
    assert metadata["test_vector"] == "cross-tenant object access"

    stored_run = client.get(f"/api/v1/tasks/{task['id']}/plan-runs/{run['id']}").json()
    assert stored_run["status"] == "completed"
    assert stored_run["decision"]["decision"] == "dispatch_explore"
    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "explore"


def test_plan_budgets_are_independent_and_snapshotted_at_claim(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    update_runtime_settings(
        client,
        reason_execute_seconds=111,
        reason_conclude_seconds=222,
        plan_execute_seconds=777,
        plan_conclude_seconds=333,
    )
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    assert run["execute_timeout_seconds"] == 777
    assert run["conclude_timeout_seconds"] == 333

    # Changing runtime settings after enqueue must affect the next claim,
    # while Reason's budgets remain irrelevant to this Plan lease.
    update_runtime_settings(
        client,
        reason_execute_seconds=999,
        reason_conclude_seconds=888,
        plan_execute_seconds=901,
        plan_conclude_seconds=402,
    )
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    stored = client.get(f"/api/v1/tasks/{task['id']}/plan-runs/{run['id']}").json()
    assert stored["execute_timeout_seconds"] == 901
    assert stored["conclude_timeout_seconds"] == 402
    assert execution["execute_timeout_seconds"] == 901
    assert execution["conclude_timeout_seconds"] == 402


def test_plan_continue_keeps_plan_phase_for_next_reconciliation(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    workspace_id = workspace.json()["id"]
    lease = {"lease_id": claimed["lease_id"]}
    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/decision",
        json={**lease, "decision": {"decision": "continue"}},
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/commit", json=lease
    )
    assert committed.status_code == 200, committed.text

    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "plan"

    reconciled = client.post("/api/v1/dispatch/plan-runs/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["created_plan_run_ids"]


def test_plan_needs_reason_creates_a_durable_reason_handoff(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    workspace_id = workspace.json()["id"]
    lease = {"lease_id": claimed["lease_id"]}
    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/decision",
        json={**lease, "decision": {"decision": "needs_reason"}},
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/commit", json=lease
    )
    assert committed.status_code == 200, committed.text
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    [reason_run_id] = reconciled.json()["created_reason_run_ids"]
    with engine.connect() as conn:
        handoff = conn.execute(
            select(
                reason_runs.c.trigger,
                reason_runs.c.trigger_payload_json,
            ).where(reason_runs.c.id == reason_run_id)
        ).mappings().one()
    assert handoff["trigger"] == "plan_handoff"
    payload = json.loads(handoff["trigger_payload_json"] or "{}")
    assert payload["plan_handoff"] is True
    assert payload["plan_run_id"] == run["id"]


def test_failed_plan_handoff_reason_run_is_not_recreated_forever(client, engine) -> None:
    """A terminal handoff failure must consume the one-shot Plan->Reason edge."""

    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    lease = {"lease_id": claimed["lease_id"]}
    workspace_id = workspace.json()["id"]
    decision = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/decision",
        json={**lease, "decision": {"decision": "needs_reason"}},
    )
    assert decision.status_code == 200, decision.text
    committed = client.post(f"/api/v1/plan-workspaces/{workspace_id}/commit", json=lease)
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
                concluded_at=now,
                updated_at=now,
                last_failure_kind="worker_backend_unavailable",
                failure_class="transient",
            )
        )

    second = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert second.status_code == 200, second.text
    assert second.json()["created_reason_run_ids"] == []


def test_pending_independent_plan_is_cancelled_when_reason_input_arrives(client, engine) -> None:
    """A new incremental Fact takes precedence over an unclaimed Plan queue item."""

    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    plan_run_id = triggered.json()["id"]
    with engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f-plan-arrival",
                task_id=task["id"],
                description="new input while Plan waits",
                created_at=utcnow(),
                archived=False,
            )
        )

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile", json={})
    assert reconciled.status_code == 200, reconciled.text
    with engine.connect() as conn:
        plan_status = conn.execute(
            select(plan_runs.c.status).where(
                plan_runs.c.task_id == task["id"], plan_runs.c.id == plan_run_id
            )
        ).scalar_one()
    assert plan_status == "cancelled"



def test_plan_historical_fact_is_visible_without_plan_consumption_marker(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    claimed, execution = _claim_plan(client, task["id"], triggered.json()["id"])
    manifest_dir = Path(execution["context_manifest_path"]).parent
    graph = json.loads((manifest_dir / "graph.json").read_text(encoding="utf-8"))
    fact = next(item for item in graph["facts"] if item["id"] == "f001")
    assert fact["consumed_by_plan_runs"] == []
    assert fact["consumed_by_reason_runs"]


def test_same_plan_snapshot_coalesces_provenance_in_the_matching_pending_run(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    first = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "Alice", "trigger": "manual"},
    )
    second = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "Bob", "trigger": "manual"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    payload = client.get(
        f"/api/v1/tasks/{task['id']}/plan-runs/{first.json()['id']}"
    ).json()["trigger_payload"]
    assert payload["coalesced_count"] == 1
    assert payload["trigger_sources"] == ["manual"]
    assert [item["payload"]["actor"] for item in payload["coalesced_triggers"]] == [
        "Alice",
        "Bob",
    ]


def test_plan_queue_is_fifo_and_plan_limit_does_not_use_reason_limit(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    update_runtime_settings(client, reason_queue_limit=1, plan_queue_limit=3)

    run_ids: list[str] = []
    for index in range(3):
        response = client.post(
            f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
            json={"actor": f"operator-{index}", "trigger": "manual"},
        )
        assert response.status_code == 200, response.text
        run_ids.append(response.json()["id"])
        if index < 2:
            with engine.begin() as conn:
                conn.execute(
                    insert(facts).values(
                        id=f"plan-f{index + 2}",
                        task_id=task["id"],
                        description=f"Plan queue signal {index + 2}",
                        created_at=utcnow(),
                        archived=False,
                    )
                )

    queued = client.get(f"/api/v1/tasks/{task['id']}").json()["queued_plan_runs"]
    assert [item["id"] for item in queued] == run_ids


def test_plan_rejects_invalid_fact_before_workspace_commit(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    run = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    ).json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace_response = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert workspace_response.status_code == 200, workspace_response.text
    workspace = workspace_response.json()
    invalid = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/intents",
        json={
            "lease_id": claimed["lease_id"],
            "intent": {
                "todo_id": "missing",
                "from_facts": ["does-not-exist"],
                "description": "invalid",
                "why_now": "invalid",
                "expected_signal": "invalid",
                "stop_condition": "invalid",
            },
        },
    )
    assert invalid.status_code in {400, 404, 409}
    assert client.get(f"/api/v1/plan-workspaces/{workspace['id']}").json()["draft"]["intent_proposals"] == []


def test_plan_rejects_unknown_source_reference_kind(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    run = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    ).json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    ).json()
    lease = {"lease_id": claimed["lease_id"]}
    todo = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/todos",
        json={
            **lease,
            "todo": {
                "ref": "source-validation",
                "title": "Validate Plan provenance",
                "category": "other",
                "sources": [{"type": "fact", "id": "f001"}],
            },
        },
    )
    assert todo.status_code == 200, todo.text

    response = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/intents",
        json={
            **lease,
            "intent": {
                "todo_ref": "source-validation",
                "source_refs": ["invented:anything"],
                "description": "Attempt with forged provenance",
                "why_now": "validate source references",
                "expected_signal": "a real source exists",
                "stop_condition": "source is checked",
            },
        },
    )

    assert response.status_code == 409, response.text
    assert "Unsupported Plan source reference kind" in response.text


def test_plan_accepts_supported_provenance_reference_kinds(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    now = utcnow()
    with engine.begin() as conn:
        artifact_rows = conn.execute(
            select(artifacts.c.id, artifacts.c.artifact_type).where(
                artifacts.c.task_id == task["id"]
            )
        ).mappings().all()
        artifact_by_type = {row["artifact_type"]: row["id"] for row in artifact_rows}
        conn.execute(
            insert(artifacts).values(
                id="a-plan-evidence",
                task_id=task["id"],
                intent_id="i001",
                review_run_id=None,
                artifact_type="explore.evidence",
                storage_key="plan/source-validation/evidence.txt",
                summary="Plan source validation evidence",
                content_hash="0" * 64,
                size=1,
                created_by="test",
                created_at=now,
                archived=False,
            )
        )
        conn.execute(
            insert(task_hints).values(
                id="h-plan-source",
                task_id=task["id"],
                content="Review the organization capability tree",
                creator="test",
                created_at=now,
                archived=False,
            )
        )
        conn.execute(
            insert(project_knowledge).values(
                id="k-plan-source",
                project_id=task["project_id"],
                title="Plan source knowledge",
                content="The organization API is shared by multiple modules",
                category="note",
                created_by="test",
                created_at=now,
                updated_at=now,
                enabled=True,
                archived=False,
            )
        )
        conn.execute(
            insert(goal_evaluations).values(
                id="ge-plan-source",
                task_id=task["id"],
                goal_cycle_id=None,
                intent_id=None,
                reason_run_id=None,
                workspace_id=None,
                achieved=False,
                reason="The goal still needs broader coverage",
                evidence_refs_json=json.dumps(["fact:f001"]),
                evaluator="test",
                created_at=now,
            )
        )

    run = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    ).json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    ).json()
    lease = {"lease_id": claimed["lease_id"]}
    todo = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/todos",
        json={
            **lease,
            "todo": {
                "ref": "source-validation",
                "title": "Validate Plan provenance",
                "category": "other",
                "sources": [{"type": "fact", "id": "f001"}],
            },
        },
    )
    assert todo.status_code == 200, todo.text
    refs = [
        "fact:f001",
        "intent:i001",
        "todo:source-validation",
        f"artifact:{artifact_by_type['bootstrap.asset_inventory']}",
        f"finding:{artifact_by_type['bootstrap.findings']}",
        f"lead:{artifact_by_type['bootstrap.initial_leads']}",
        "evidence:a-plan-evidence",
        "hint:h-plan-source",
        "knowledge:k-plan-source",
        "goal_evaluation:ge-plan-source",
    ]
    response = client.post(
        f"/api/v1/plan-workspaces/{workspace['id']}/intents",
        json={
            **lease,
            "intent": {
                "todo_ref": "source-validation",
                "source_refs": refs,
                "description": "Use all supported provenance references",
                "why_now": "validate source references",
                "expected_signal": "a real source is accepted",
                "stop_condition": "all references are checked",
            },
        },
    )
    assert response.status_code == 200, response.text


def test_plan_rejects_archived_intent_source_references(client, engine) -> None:
    """New Explore work may cite only active provenance records.

    Plan still receives archived history in its Context, but archived rows are
    review material rather than valid inputs for a newly dispatched Intent.
    GoalEvaluation has no archived flag and remains immutable historical
    evidence, so it is intentionally not included in this active-source set.
    """

    task = _task_with_completed_bootstrap(client, engine)
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f-plan-active-source",
                task_id=task["id"],
                description="Active Plan source Fact",
                created_at=now,
                archived=False,
            )
        )
        conn.execute(
            insert(facts).values(
                id="f-plan-archived-source",
                task_id=task["id"],
                description="Archived Plan source Fact",
                created_at=now,
                archived=True,
            )
        )
        conn.execute(
            update(intents)
            .where(intents.c.task_id == task["id"], intents.c.id == "i001")
            .values(archived=True)
        )
        for artifact_id, artifact_type in (
            ("a-plan-archived-artifact", "bootstrap.asset_inventory"),
            ("a-plan-archived-finding", "explore.findings"),
            ("a-plan-archived-lead", "explore.leads"),
            ("a-plan-archived-evidence", "explore.evidence"),
        ):
            conn.execute(
                insert(artifacts).values(
                    id=artifact_id,
                    task_id=task["id"],
                    intent_id=None,
                    review_run_id=None,
                    artifact_type=artifact_type,
                    storage_key=f"plan/archive/{artifact_id}.txt",
                    summary="Archived Plan source",
                    content_hash="0" * 64,
                    size=1,
                    created_by="test",
                    created_at=now,
                    archived=True,
                )
            )
        conn.execute(
            insert(task_hints).values(
                id="h-plan-archived-source",
                task_id=task["id"],
                content="Archived Plan hint",
                creator="test",
                created_at=now,
                archived=True,
            )
        )
        conn.execute(
            insert(project_knowledge).values(
                id="k-plan-archived-source",
                project_id=task["project_id"],
                title="Archived Plan knowledge",
                content="Archived Plan source",
                category="note",
                created_by="test",
                created_at=now,
                updated_at=now,
                enabled=True,
                archived=True,
            )
        )
        for todo_id, archived in (
            ("td-plan-active-source", False),
            ("td-plan-archived-source", True),
        ):
            conn.execute(
                insert(todos).values(
                    id=todo_id,
                    task_id=task["id"],
                    title=todo_id,
                    category="recon",
                    theme="",
                    priority=50,
                    status="open",
                    created_by="test",
                    status_reason="",
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                    archived=archived,
                )
            )

    run = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    ).json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    ).json()

    archived_refs = [
        "fact:f-plan-archived-source",
        "intent:i001",
        "todo:td-plan-archived-source",
        "artifact:a-plan-archived-artifact",
        "finding:a-plan-archived-finding",
        "lead:a-plan-archived-lead",
        "evidence:a-plan-archived-evidence",
        "hint:h-plan-archived-source",
        "knowledge:k-plan-archived-source",
    ]
    for source_ref in archived_refs:
        response = client.post(
            f"/api/v1/plan-workspaces/{workspace['id']}/intents",
            json={
                "lease_id": claimed["lease_id"],
                "intent": {
                    "todo_id": "td-plan-active-source",
                    "from_facts": ["f-plan-active-source"],
                    "source_refs": [source_ref],
                    "description": "Should reject archived provenance",
                    "why_now": "Archived source must remain historical only",
                    "expected_signal": "No archived source is accepted",
                    "stop_condition": "Stop after source validation",
                },
            },
        )
        assert response.status_code == 409, (source_ref, response.text)
        assert "source reference not found" in response.text

    stored = client.get(f"/api/v1/plan-workspaces/{workspace['id']}").json()
    assert stored["draft"]["intent_proposals"] == []


def test_legacy_reason_plan_rows_are_read_only(client, engine) -> None:
    """A pre-split Plan row must not be claimable through the Reason API."""

    task = _task_with_completed_bootstrap(client, engine)
    with engine.begin() as conn:
        reason_run_id = conn.execute(
            select(reason_runs.c.id)
            .where(reason_runs.c.task_id == task["id"])
            .order_by(reason_runs.c.created_at.desc(), reason_runs.c.id.desc())
            .limit(1)
        ).scalar_one()
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == reason_run_id)
            .values(trigger="plan", status="pending", worker=None, lease_id=None)
        )

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}/claim",
        json={"worker_name": "plan-worker"},
    )
    assert response.status_code == 409, response.text
    assert "read-only" in response.text


def test_task_api_keeps_plan_queue_out_of_reason_queue(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()

    queued = client.get(f"/api/v1/tasks/{task['id']}")
    assert queued.status_code == 200, queued.text
    queued_payload = queued.json()
    assert queued_payload["active_reason_run"] is None
    assert queued_payload["active_plan_run"] is None
    assert queued_payload["queued_reason_runs"] == []
    assert [item["id"] for item in queued_payload["queued_plan_runs"]] == [run["id"]]

    claimed, _ = _claim_plan(client, task["id"], run["id"])
    active = client.get(f"/api/v1/tasks/{task['id']}")
    assert active.status_code == 200, active.text
    active_payload = active.json()
    assert active_payload["active_reason_run"] is None
    assert active_payload["active_plan_run"]["id"] == run["id"]
    assert active_payload["active_plan_run"]["status"] == "running"
    assert active_payload["queued_reason_runs"] == []
    assert active_payload["queued_plan_runs"] == []


def test_dispatch_snapshot_counts_active_plan_worker_once(client, engine) -> None:
    """A Plan lease consumes one shared Worker slot, not two."""

    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()

    _claim_plan(client, task["id"], run["id"])
    snapshot = client.get("/api/v1/dispatch/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    worker = next(item for item in snapshot.json()["workers"] if item["name"] == "plan-worker")
    assert worker["running"] == 1


def test_terminal_transient_plan_failure_requires_manual_recovery(client, engine) -> None:
    """A terminal infrastructure failure must not create replacement Runs forever."""

    task = _task_with_completed_bootstrap(client, engine)
    update_runtime_settings(client, infrastructure_retry_limit=1)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    claimed, _ = _claim_plan(client, task["id"], run["id"])

    released = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{run['id']}/release",
        json={
            "lease_id": claimed["lease_id"],
            "failure_kind": "worker_backend_unavailable",
            "failure_class": "transient",
        },
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "failed"

    for _ in range(2):
        reconciled = client.post("/api/v1/dispatch/plan-runs/reconcile")
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["created_plan_run_ids"] == []

    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "plan"
    with engine.connect() as conn:
        rows = conn.execute(
            select(plan_runs.c.id, plan_runs.c.status).where(
                plan_runs.c.task_id == task["id"]
            )
        ).all()
    assert rows == [(run["id"], "failed")]

    manual = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "operator", "trigger": "manual-recovery"},
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["id"] != run["id"]


def test_plan_input_keeps_snapshot_and_excludes_fact_reserved_by_reason(client, engine) -> None:
    """Late-bound Plan input owns only Facts not already consumed by Reason."""

    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    plan_run = triggered.json()

    with engine.begin() as conn:
        conn.execute(
            insert(facts).values(
                id="f002",
                task_id=task["id"],
                description="排队期间产生、随后被 Reason 预留的新 Fact",
                created_at=utcnow(),
                archived=False,
            )
        )
    reason_trigger = client.post(
        f"/api/v1/tasks/{task['id']}/reason-runs/trigger",
        json={"actor": "reason-queue"},
    )
    assert reason_trigger.status_code == 200, reason_trigger.text

    # The independent Plan cannot claim while a Reason item is pending.  In
    # production this transition is performed by the Reason worker; release
    # the synthetic fixture here so the test can exercise Plan's late-binding
    # ownership rule after the queue barrier has cleared.
    reason_run_id = reason_trigger.json()["id"]
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.id == reason_run_id,
            )
            .values(status="completed", concluded_at=now, updated_at=now)
        )

    claimed, _ = _claim_plan(client, task["id"], plan_run["id"])
    execution = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan_run['id']}/input",
        json={"lease_id": claimed["lease_id"]},
    )
    assert execution.status_code == 200, execution.text
    # The bootstrap Fact was already reserved and completed by the initial
    # ReasonRun before this Plan was queued.  Plan still sees it in the full
    # historical Context, but it must not claim it through plan_run_sources.
    assert execution.json()["source_fact_ids"] == []


def test_plan_intent_uses_explore_budget_and_scope_validation(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    triggered = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    )
    assert triggered.status_code == 200, triggered.text
    run = triggered.json()
    claimed, execution = _claim_plan(client, task["id"], run["id"])
    workspace = client.post(
        f"/api/v1/tasks/{task['id']}/plan/workspaces",
        json={
            "plan_run_id": run["id"],
            "lease_id": claimed["lease_id"],
            "context_revision": execution["context_revision"],
        },
    )
    assert workspace.status_code == 200, workspace.text
    workspace_id = workspace.json()["id"]
    todo = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/todos",
        json={
            "lease_id": claimed["lease_id"],
            "todo": {
                "ref": "budget-check",
                "title": "Budget check",
                "category": "recon",
                "sources": [{"type": "fact", "id": "f001"}],
            },
        },
    )
    assert todo.status_code == 200, todo.text
    invalid = client.post(
        f"/api/v1/plan-workspaces/{workspace_id}/intents",
        json={
            "lease_id": claimed["lease_id"],
            "intent": {
                "todo_ref": "budget-check",
                "from_facts": ["f001"],
                "source_refs": ["fact:f001"],
                "description": "Invalid budget should be rejected",
                "targets": ["a.example.com"],
                "endpoint": "/health",
                "test_vector": "baseline",
                "why_now": "budget validation regression",
                "expected_signal": "none",
                "stop_condition": "stop",
                "execute_seconds": 1,
                "conclude_seconds": 1,
            },
        },
    )
    assert invalid.status_code == 409, invalid.text
    assert "execute_seconds" in invalid.text


def test_plan_claim_waits_behind_pending_reason_queue(client, engine) -> None:
    task = _task_with_completed_bootstrap(client, engine)
    reason = client.post(
        f"/api/v1/tasks/{task['id']}/reason-runs/trigger",
        json={"actor": "reason-first"},
    )
    assert reason.status_code == 200, reason.text
    plan = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"trigger": "manual"},
    )
    assert plan.status_code == 200, plan.text
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/plan-runs/{plan.json()['id']}/claim",
        json={"worker_name": "plan-worker"},
    )
    assert claim.status_code == 409, claim.text
    assert "ReasonRun" in claim.text or "Reason" in claim.text


def test_reason_reconcile_preserves_pending_and_active_plan_phase(client, engine) -> None:
    """Reason reconciliation must not take ownership from independent Plan."""

    task = _task_with_completed_bootstrap(client, engine)
    run = client.post(
        f"/api/v1/tasks/{task['id']}/plan-runs/trigger",
        json={"actor": "test", "trigger": "manual"},
    ).json()

    # Reproduce a stale visible phase from an earlier control-plane tick.  A
    # queued independent Plan is still the next panorama owner; Reason must
    # not route the Task back to waiting or manufacture a legacy Plan row.
    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(phase="waiting")
        )
    pending_reconcile = client.post(
        "/api/v1/dispatch/reason-runs/reconcile",
        json={},
    )
    assert pending_reconcile.status_code == 200, pending_reconcile.text
    assert pending_reconcile.json()["created_reason_run_ids"] == []
    pending_task = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert pending_task["phase"] == "plan"
    assert [item["id"] for item in pending_task["queued_plan_runs"]] == [run["id"]]

    _claim_plan(client, task["id"], run["id"])
    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(phase="waiting")
        )
    active_reconcile = client.post(
        "/api/v1/dispatch/reason-runs/reconcile",
        json={},
    )
    assert active_reconcile.status_code == 200, active_reconcile.text
    assert active_reconcile.json()["created_reason_run_ids"] == []
    active_task = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert active_task["phase"] == "plan"
    assert active_task["active_plan_run"]["id"] == run["id"]

    with engine.connect() as conn:
        legacy_plan_count = conn.execute(
            select(reason_runs.c.id).where(
                reason_runs.c.task_id == task["id"],
                reason_runs.c.trigger == "plan",
            )
        ).scalars().all()
    assert legacy_plan_count == []
