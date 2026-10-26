"""Diagnostics must distinguish "waiting" from "will not move on its own".

A parked row and a cooling-down Worker both look like "nothing is happening",
and they need different fixes.  The diagnostics payload is where an operator
looks first, so it has to name which one it is.
"""

from __future__ import annotations

from sqlalchemy import select


RUN_ID = "rr000499"


def _task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "诊断"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "停摆诊断",
            "origin": "a.example.com",
            "goal": "验证诊断输出",
        },
    ).json()


def test_a_parked_run_is_reported_with_its_recovery_progress(client, engine) -> None:
    from axiom.modules.reason.tables import reason_runs

    task = _task(client)
    with engine.begin() as conn:
        conn.execute(
            reason_runs.insert().values(
                id=RUN_ID,
                task_id=task["id"],
                status="pending",
                trigger="scheduler",
                trigger_payload_json="{}",
                input_fingerprint="",
                worker_pool_json="[]",
                settings_version=1,
                execute_timeout_seconds=600,
                conclude_timeout_seconds=300,
                explore_execute_min_seconds=300,
                explore_execute_max_seconds=3600,
                explore_conclude_min_seconds=180,
                explore_conclude_max_seconds=600,
                retry_count=0,
                infrastructure_retry_count=3,
                next_retry_at=None,
                retry_blocked_at="2026-10-26T11:40:00Z",
                recovery_round=1,
                last_failure_kind="lease_expired",
                failure_class="transient",
                activity_kind="retry_blocked",
                activity_summary="Reason 基础设施重试已达上限",
                created_at="2026-10-26T10:00:00Z",
                updated_at="2026-10-26T11:40:00Z",
            )
        )

    payload = client.get(f"/api/v1/tasks/{task['id']}/diagnostics").json()

    recovery = payload["retry_recovery"]
    assert recovery["enabled"] is True
    assert recovery["infrastructure_retry_limit"] == 3
    parked = {item["id"]: item for item in recovery["parked"]}
    assert RUN_ID in parked
    row = parked[RUN_ID]
    assert row["phase"] == "reason"
    assert row["recovery_round"] == 1
    assert row["last_failure_kind"] == "lease_expired"
    assert row["needs_human"] is False
    assert recovery["needs_human"] == []


def test_an_exhausted_recovery_is_flagged_for_a_human(client, engine) -> None:
    from axiom.modules.reason.tables import reason_runs

    task = _task(client)
    with engine.begin() as conn:
        conn.execute(
            reason_runs.insert().values(
                id=RUN_ID,
                task_id=task["id"],
                status="pending",
                trigger="scheduler",
                trigger_payload_json="{}",
                input_fingerprint="",
                worker_pool_json="[]",
                settings_version=1,
                execute_timeout_seconds=600,
                conclude_timeout_seconds=300,
                explore_execute_min_seconds=300,
                explore_execute_max_seconds=3600,
                explore_conclude_min_seconds=180,
                explore_conclude_max_seconds=600,
                retry_count=0,
                infrastructure_retry_count=3,
                next_retry_at=None,
                retry_blocked_at="2026-10-26T11:40:00Z",
                # Automatic recovery has already spent its budget.
                recovery_round=5,
                last_failure_kind="worker_infrastructure",
                failure_class="transient",
                activity_kind="retry_blocked",
                activity_summary="Reason 基础设施重试已达上限",
                created_at="2026-10-26T10:00:00Z",
                updated_at="2026-10-26T11:40:00Z",
            )
        )

    payload = client.get(f"/api/v1/tasks/{task['id']}/diagnostics").json()

    recovery = payload["retry_recovery"]
    assert [item["id"] for item in recovery["needs_human"]] == [RUN_ID]
    assert recovery["parked"][0]["needs_human"] is True


def test_a_healthy_task_reports_no_parked_work(client) -> None:
    task = _task(client)

    recovery = client.get(f"/api/v1/tasks/{task['id']}/diagnostics").json()[
        "retry_recovery"
    ]

    assert recovery["parked"] == []
    assert recovery["needs_human"] == []
    assert recovery["breakers"] == []


def test_a_tripped_worker_appears_in_the_diagnostics(client, engine) -> None:
    from axiom.modules.workers.tables import workers

    task = _task(client)
    client.put(
        "/api/v1/workers/diag-worker",
        json={"capabilities": ["reason"], "max_running": 1},
    )
    with engine.begin() as conn:
        conn.execute(
            workers.update()
            .where(workers.c.name == "diag-worker")
            .values(trip_count=2, transient_failure_count=1,
                    cooldown_until="2099-01-01T00:00:00Z",
                    last_failure_kind="worker_infrastructure")
        )

    recovery = client.get(f"/api/v1/tasks/{task['id']}/diagnostics").json()[
        "retry_recovery"
    ]

    breaker = next(item for item in recovery["breakers"] if item["worker"] == "diag-worker")
    assert breaker["trip_count"] == 2
    assert breaker["tripped"] is True
    assert breaker["last_failure_kind"] == "worker_infrastructure"
