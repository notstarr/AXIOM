"""Automatic recovery of parked work items (``dispatch.recovery``).

A parked row is one whose infrastructure retries are exhausted: it stays
``pending`` with no ``next_retry_at``, so no dispatch tick will ever claim it.
Before this pass existed, only an operator with a shell could bring one back —
which is how a twenty-minute provider outage parked a Task for hours.

The tests pin the three properties that make the pass safe to run every tick:
it waits for a quiet window, it stops after a bounded number of rounds, and it
never touches a Task the operator stopped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from axiom.modules.dispatch.recovery import recover_parked
from axiom.modules.audit.service import AuditService
from axiom.modules.graph.tables import intents
from axiom.modules.reason.tables import reason_runs


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class _Config:
    retry_recovery_enabled = True
    retry_recovery_quiet_seconds = 900
    retry_recovery_max_rounds = 2
    infrastructure_retry_limit = 3


def _now() -> str:
    return NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(delta_seconds: int) -> str:
    return (NOW - timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task(client) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Recovery"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "自动恢复",
            "origin": "a.example.com",
            "goal": "验证自动恢复",
        },
    ).json()


RUN_ID = "rr000499"


def _park(client, engine, task_id: str, *, quiet_seconds: int, rounds: int = 0) -> str:
    """Insert a pending ReasonRun and leave it parked, as a failed run would."""

    with engine.begin() as conn:
        conn.execute(
            reason_runs.insert().values(
                id=RUN_ID,
                task_id=task_id,
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
                retry_blocked_at=_stamp(quiet_seconds),
                recovery_round=rounds,
                last_failure_kind="worker_infrastructure",
                failure_class="transient",
                activity_kind="retry_blocked",
                activity_summary="Reason 基础设施重试已达上限，等待人工或系统恢复",
                created_at=_stamp(7200),
                updated_at=_stamp(1200),
            )
        )
    return RUN_ID


def _run(engine) -> object:
    with engine.begin() as conn:
        return recover_parked(
            conn, audit=AuditService(engine), config=_Config(), now=_now()
        )


def _row(engine, task_id: str, run_id: str):
    with engine.connect() as conn:
        return conn.execute(
            select(reason_runs).where(
                reason_runs.c.task_id == task_id, reason_runs.c.id == run_id
            )
        ).mappings().one()


def test_a_row_parked_long_enough_is_re_armed(client, engine) -> None:
    task = _task(client)
    run_id = _park(client, engine, task["id"], quiet_seconds=1200)

    outcome = _run(engine)

    assert [item.run_id for item in outcome.recovered] == [run_id]
    row = _row(engine, task["id"], run_id)
    assert row["infrastructure_retry_count"] == 0
    assert row["retry_blocked_at"] is None
    assert row["next_retry_at"] is None
    assert row["recovery_round"] == 1
    assert row["status"] == "pending"
    # The stale failure text must not survive a recovery that says it succeeded.
    assert row["activity_kind"] == ""
    assert row["last_failure_kind"] == ""


def test_a_row_parked_inside_the_quiet_window_is_left_alone(client, engine) -> None:
    """Recovery must not race the retry budget it is trying to respect."""

    task = _task(client)
    run_id = _park(client, engine, task["id"], quiet_seconds=60)

    outcome = _run(engine)

    assert outcome.recovered == []
    row = _row(engine, task["id"], run_id)
    assert row["infrastructure_retry_count"] == 3
    assert row["recovery_round"] == 0


def test_recovery_stops_at_the_round_limit_and_announces_it_once(client, engine) -> None:
    task = _task(client)
    run_id = _park(client, engine, task["id"], quiet_seconds=1200, rounds=2)

    first = _run(engine)
    assert first.recovered == []
    assert [item.run_id for item in first.exhausted] == [run_id]

    # A second pass must stay silent: the announcement is the transition, not a
    # per-tick heartbeat, or the log would fill with the same warning forever.
    second = _run(engine)
    assert second.exhausted == []
    assert second.recovered == []

    row = _row(engine, task["id"], run_id)
    assert row["infrastructure_retry_count"] == 3
    assert row["recovery_round"] > 2


def test_the_end_to_end_path_returns_a_recovered_row_to_the_queue(client, engine) -> None:
    """The point of recovery: the row becomes claimable again, without a human.

    A unit-level assertion that the counters were reset would pass even if the
    dispatcher still refused the row; this asks the claim endpoint directly.
    """

    task = _task(client)
    # The endpoint uses the real clock, so park it unambiguously in the past.
    run_id = _park(client, engine, task["id"], quiet_seconds=10**8)
    client.put(
        "/api/v1/workers/recovery-worker",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 2},
    )

    before = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{run_id}/claim",
        json={"worker_name": "recovery-worker"},
    )
    assert before.status_code == 409

    client.post("/api/v1/dispatch/recover-parked")

    after = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{run_id}/claim",
        json={"worker_name": "recovery-worker"},
    )
    assert after.status_code == 200, after.text
    assert after.json()["reason_run_id"] == run_id


def test_a_paused_task_keeps_its_parked_rows(client, engine) -> None:
    """Recovery must not resurrect work the operator deliberately stopped."""

    task = _task(client)
    run_id = _park(client, engine, task["id"], quiet_seconds=1200)
    client.post(f"/api/v1/tasks/{task['id']}/stop")

    outcome = _run(engine)

    assert outcome.recovered == []
    assert outcome.exhausted == []
    row = _row(engine, task["id"], run_id)
    assert row["infrastructure_retry_count"] == 3
    assert row["recovery_round"] == 0


def test_recovery_can_be_switched_off(client, engine) -> None:
    class _Off(_Config):
        retry_recovery_enabled = False

    task = _task(client)
    run_id = _park(client, engine, task["id"], quiet_seconds=1200)
    with engine.begin() as conn:
        outcome = recover_parked(
            conn, audit=AuditService(engine), config=_Off(), now=_now()
        )

    assert outcome.empty()
    assert _row(engine, task["id"], run_id)["infrastructure_retry_count"] == 3


def test_recovery_writes_an_audit_record(client, engine) -> None:
    task = _task(client)
    run_id = _park(client, engine, task["id"], quiet_seconds=1200)

    _run(engine)

    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": task["id"], "event_type": "reason_run.auto_recovered"},
    ).json()
    assert events, "recovery must leave a trace the operator can find"
    assert events[0]["reason_run_id"] == run_id
    payload = events[0]["payload"]
    assert payload["phase"] == "reason"
    assert payload["previous_infrastructure_retry_count"] == 3
    assert payload["worker_pool"] == []
    assert payload["recovery_round"] == 1
    assert payload["last_failure_kind"] == "worker_infrastructure"
    assert payload["quiet_seconds"] == 900


def _park_intent(engine, task_id: str, *, quiet_seconds: int) -> str:
    """Insert a parked Explore Intent.

    ``intents`` is the odd one out of the four parked queues: it has never had an
    ``updated_at`` column.  A recovery pass that assumes a shared column set
    therefore fails to even compile its UPDATE for this queue, and because the
    scan runs in the caller's transaction the failure takes Reason, Plan and
    Review recovery down with it.  That is not hypothetical — it stalled a live
    Task for hours.  Keep this fixture deliberately minimal so the missing column
    stays missing.
    """

    run_id = "i000901"
    with engine.begin() as conn:
        conn.execute(
            intents.insert().values(
                id=run_id,
                task_id=task_id,
                kind="explore",
                description="parked Intent",
                priority=50,
                status="pending",
                creator="reason.test",
                worker_pool_json="[]",
                targets_json="[]",
                dedup_fingerprint="",
                plan_metadata_json="{}",
                execute_timeout_seconds=600,
                conclude_timeout_seconds=300,
                retry_count=0,
                infrastructure_retry_count=3,
                next_retry_at=None,
                retry_blocked_at=_stamp(quiet_seconds),
                recovery_round=0,
                last_failure_kind="worker_infrastructure",
                failure_class="transient",
                activity_kind="retry_blocked",
                activity_summary="Intent 基础设施重试已达上限",
                created_at=_stamp(7200),
                archived=False,
            )
        )
    return run_id


def _intent_row(engine, task_id: str, run_id: str):
    with engine.connect() as conn:
        return conn.execute(
            select(intents).where(
                intents.c.task_id == task_id, intents.c.id == run_id
            )
        ).mappings().one()


def test_a_parked_intent_is_re_armed_even_though_it_has_no_updated_at(
    client, engine
) -> None:
    task = _task(client)
    run_id = _park_intent(engine, task["id"], quiet_seconds=1200)

    outcome = _run(engine)

    assert [item.run_id for item in outcome.recovered] == [run_id]
    assert outcome.failed_queues == []
    row = _intent_row(engine, task["id"], run_id)
    assert row["infrastructure_retry_count"] == 0
    assert row["retry_blocked_at"] is None
    assert row["recovery_round"] == 1


def test_a_failing_queue_does_not_stop_the_others(client, engine, monkeypatch) -> None:
    """One unreadable queue must degrade to a report, not to a dead Task."""

    task = _task(client)
    intent_id = _park_intent(engine, task["id"], quiet_seconds=1200)
    run_id = _park(client, engine, task["id"], quiet_seconds=1200)

    from axiom.modules.dispatch import recovery as recovery_module

    real = recovery_module._reset_values

    def explode(table, *, now, recovery_round, config):
        if table.name == "intents":
            raise RuntimeError("simulated schema drift")
        return real(table, now=now, recovery_round=recovery_round, config=config)

    monkeypatch.setattr(recovery_module, "_reset_values", explode)

    outcome = _run(engine)

    # The intent queue is reported as failed...
    assert any(entry.startswith("intent:") for entry in outcome.failed_queues)
    assert intent_id not in [item.run_id for item in outcome.recovered]
    # ...while the other queues still recover in the same pass.
    assert [item.run_id for item in outcome.recovered] == [run_id]
    assert _row(engine, task["id"], run_id)["recovery_round"] == 1
