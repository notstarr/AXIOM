"""Task-level bulk reset of the retry budget across every dispatch queue.

The operator-facing problem is "this Task is stuck", not "this Intent is
parked".  These cases pin the Task-level button: one call clears the retry
budget of the Intent, ReasonRun, PlanRun and ReviewRun queues at once, so a
worker can pick the work up again immediately instead of waiting out the
recovery quiet period.

The rules that matter:

* only *pending* work is touched — a running item owns a live lease, and a
  completed item has no budget left;
* only items that actually carry retry debris are counted, so the report says
  what was unblocked rather than what was merely looked at;
* the reset clears the whole debris set (counters, scheduled retry, parked
  timestamp, recovery round, failure classification), not just one counter.
"""

from __future__ import annotations

from sqlalchemy import insert

from axiom.modules.graph.tables import intents
from axiom.modules.plan.tables import plan_runs
from axiom.modules.reason.tables import reason_runs
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs


def _project_and_task(client) -> tuple[dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "任务级预算重置"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "任务级重试预算恢复",
            "origin": "retry.example.com",
        },
    ).json()
    return project, task


def _debris(**overrides) -> dict:
    """The state a parked work item is left in by the recovery machinery."""

    values = {
        "retry_count": 4,
        "infrastructure_retry_count": 5,
        "last_failure_kind": "worker_infrastructure",
        "failure_class": "transient",
        "next_retry_at": None,
        "retry_blocked_at": "2026-11-03T00:00:00Z",
        "recovery_round": 2,
    }
    values.update(overrides)
    return values


def _park(engine, table, task_id: str, row_id: str, **overrides) -> None:
    """Insert one pending work item already carrying retry debris.

    Each queue has its own set of required columns, so the shared scaffolding
    is built per table rather than by reusing one INSERT.
    """

    base: dict = {"id": row_id, "task_id": task_id, "status": "pending"}
    if table is intents:
        base.update(
            kind="explore",
            description="待执行的探索意图",
            creator="worker-a",
            created_at="2026-11-03T00:00:00Z",
        )
    elif table is reason_runs:
        base.update(
            trigger="scheduler",
            input_fingerprint="fp-reason",
            settings_version=1,
            execute_timeout_seconds=300,
            conclude_timeout_seconds=120,
            explore_execute_min_seconds=60,
            explore_execute_max_seconds=300,
            explore_conclude_min_seconds=30,
            explore_conclude_max_seconds=120,
            created_at="2026-11-03T00:00:00Z",
            updated_at="2026-11-03T00:00:00Z",
        )
    elif table is plan_runs:
        base.update(
            trigger="reconcile",
            input_fingerprint="fp-plan",
            settings_version=1,
            execute_timeout_seconds=300,
            conclude_timeout_seconds=120,
            created_at="2026-11-03T00:00:00Z",
            updated_at="2026-11-03T00:00:00Z",
        )
    else:  # vulnerability_review_runs
        base.update(
            trigger="manual",
            input_fingerprint="fp-review",
            settings_version=1,
            execute_timeout_seconds=300,
            conclude_timeout_seconds=120,
            # A review run must name the subject it reviews.  A RedTeam Result
            # is the cheaper subject to stand up here than a Vulnerability.
            review_subject_type="redteam_result",
            red_team_result_id="x900",
            created_at="2026-11-03T00:00:00Z",
            updated_at="2026-11-03T00:00:00Z",
        )
    base.update(_debris(**overrides))
    with engine.begin() as conn:
        conn.execute(insert(table).values(**base))


def _seed_redteam_result(engine, task_id: str, result_id: str = "x900") -> None:
    """A minimal RedTeam Result, so a review run has a subject to point at."""
    from axiom.modules.redteam.tables import red_team_chains, red_team_results

    with engine.begin() as conn:
        conn.execute(
            insert(red_team_chains).values(
                id="ch900",
                task_id=task_id,
                title="链",
                initial_principal_json={"id": "user:member"},
                target_capabilities_json=[],
                target_assets_json=[],
                status="open",
                status_reason="",
                current_state_json={},
                source_fact_ids_json=[],
                source_intent_ids_json=[],
                created_by="worker-a",
                created_at="2026-11-03T00:00:00Z",
                updated_at="2026-11-03T00:00:00Z",
            )
        )
        conn.execute(
            insert(red_team_results).values(
                id=result_id,
                task_id=task_id,
                chain_id="ch900",
                intent_id="i900",
                result_type="permission",
                title="成果",
                description="描述",
                principal="user:member",
                tenant="",
                capabilities_before_json=[],
                capabilities_gained_json=["tenant:admin"],
                capabilities_after_json=[],
                target_asset="a.example.com",
                data_fields_json=[],
                data_summary="",
                evidence_refs_json=[],
                reproduction_json={},
                status="reviewing",
                reported_by="worker-a",
                points_declared=10,
                points_awarded=0,
                review_reason="",
                created_at="2026-11-03T00:00:00Z",
                updated_at="2026-11-03T00:00:00Z",
            )
        )


def test_reset_clears_every_queue_of_the_task(client, engine) -> None:
    """One call unblocks Intent, ReasonRun, PlanRun and ReviewRun together."""
    _project, task = _project_and_task(client)
    task_id = task["id"]
    _park(engine, intents, task_id, "i900")
    _park(engine, reason_runs, task_id, "rr000001")
    _park(engine, plan_runs, task_id, "pr000001")
    _seed_redteam_result(engine, task_id)
    _park(engine, vulnerability_review_runs, task_id, "vr000001")

    response = client.post(f"/api/v1/dispatch/tasks/{task_id}/reset-retry-budget")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == task_id
    assert body["total"] == 4
    assert body["reset_by_phase"] == {
        "intent": 1,
        "reason": 1,
        "plan": 1,
        "review": 1,
    }

    # Every queue must be back to a clean budget.
    with engine.connect() as conn:
        for table, row_id in (
            (intents, "i900"),
            (reason_runs, "rr000001"),
            (plan_runs, "pr000001"),
            (vulnerability_review_runs, "vr000001"),
        ):
            row = conn.execute(
                table.select().where(
                    table.c.task_id == task_id, table.c.id == row_id
                )
            ).mappings().one()
            assert row["retry_count"] == 0, row_id
            assert row["infrastructure_retry_count"] == 0, row_id
            assert row["last_failure_kind"] == "", row_id
            assert row["failure_class"] == "", row_id
            assert row["next_retry_at"] is None, row_id
            assert row["retry_blocked_at"] is None, row_id
            assert row["recovery_round"] == 0, row_id


def test_reset_makes_a_parked_item_claimable_again(client, engine) -> None:
    """The point of the button: a worker can take the work without waiting."""
    _project, task = _project_and_task(client)
    task_id = task["id"]
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "explore"], "max_running": 1},
    )
    _park(engine, intents, task_id, "i901")
    claim_endpoint = f"/api/v1/dispatch/tasks/{task_id}/intents/i901/claim"
    blocked = client.post(claim_endpoint, json={"worker_name": "worker-a"})
    assert blocked.status_code == 409

    reset = client.post(f"/api/v1/dispatch/tasks/{task_id}/reset-retry-budget")
    assert reset.status_code == 200, reset.text
    assert reset.json()["total"] == 1

    claimed = client.post(claim_endpoint, json={"worker_name": "worker-a"})
    assert claimed.status_code == 200, claimed.text


def test_reset_counts_only_items_that_were_actually_stuck(client, engine) -> None:
    """A queue that never failed is not reported as reset."""
    _project, task = _project_and_task(client)
    task_id = task["id"]
    _park(engine, intents, task_id, "i900")
    # i002 exists but never failed: no counters, no parked timestamp.
    _park(
        engine,
        intents,
        task_id,
        "i901",
        retry_count=0,
        infrastructure_retry_count=0,
        last_failure_kind="",
        failure_class="",
        retry_blocked_at=None,
        recovery_round=0,
    )

    response = client.post(f"/api/v1/dispatch/tasks/{task_id}/reset-retry-budget")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["reset_by_phase"]["intent"] == 1
    # The clean item was not touched, so it must not be counted anywhere.
    assert body["reset_by_phase"]["reason"] == 0
    assert body["reset_by_phase"]["plan"] == 0
    assert body["reset_by_phase"]["review"] == 0


def test_reset_leaves_running_work_alone(client, engine) -> None:
    """A running item owns a live lease; clearing its counters would double-book it."""
    _project, task = _project_and_task(client)
    task_id = task["id"]
    _park(engine, intents, task_id, "i900", status="running")

    response = client.post(f"/api/v1/dispatch/tasks/{task_id}/reset-retry-budget")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    with engine.connect() as conn:
        row = conn.execute(
            intents.select().where(
                intents.c.task_id == task_id, intents.c.id == "i900"
            )
        ).mappings().one()
    assert row["infrastructure_retry_count"] == 5
    assert row["retry_blocked_at"] is not None


def test_reset_does_not_reach_into_another_task(client, engine) -> None:
    """The reset is scoped to one Task."""
    _project, task = _project_and_task(client)
    other = client.post(
        "/api/v1/tasks",
        json={
            "project_id": task["project_id"],
            "title": "另一个任务",
            "origin": "other.example.com",
        },
    ).json()
    _park(engine, intents, task["id"], "i900")
    _park(engine, intents, other["id"], "i900")

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reset-retry-budget"
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    with engine.connect() as conn:
        row = conn.execute(
            intents.select().where(
                intents.c.task_id == other["id"], intents.c.id == "i900"
            )
        ).mappings().one()
    assert row["infrastructure_retry_count"] == 5


def test_reset_reports_a_missing_task(client) -> None:
    response = client.post("/api/v1/dispatch/tasks/t_999/reset-retry-budget")
    assert response.status_code == 404, response.text


def test_reset_is_idempotent(client, engine) -> None:
    """Pressing the button twice is harmless: the second press finds nothing."""
    _project, task = _project_and_task(client)
    task_id = task["id"]
    _park(engine, intents, task_id, "i900")

    first = client.post(f"/api/v1/dispatch/tasks/{task_id}/reset-retry-budget")
    second = client.post(f"/api/v1/dispatch/tasks/{task_id}/reset-retry-budget")

    assert first.status_code == 200 and first.json()["total"] == 1
    assert second.status_code == 200 and second.json()["total"] == 0
