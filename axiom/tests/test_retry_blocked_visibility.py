"""Parked work must be announced, counted, and recoverable.

A work item whose infrastructure retries hit the limit stays ``pending`` with
no retry timestamp.  The dispatcher then skips it on every tick, so the Task
silently stops producing work.  These tests cover the three things that make
that state survivable:

* the retry budget itself (boundaries and backoff),
* an audit event that says the item is parked rather than merely released,
* a way for an operator to release it again.
"""

from __future__ import annotations

from sqlalchemy import select, update

from axiom.modules.dispatch.retry import retry_outcome
from axiom.modules.reason.tables import reason_runs
from protocol_helpers import claim_reason_run
from test_reason_run_control import _bootstrap_to_reason


NOW = "2026-10-08T00:00:00Z"


class _Config:
    execution_retry_limit = 6
    infrastructure_retry_limit = 3
    retry_base_seconds = 30
    retry_max_seconds = 300


def _outcome(attempt: int, failure_class: str, config=None):
    return retry_outcome(
        config or _Config(), attempt=attempt, failure_class=failure_class, now=NOW
    )


# --------------------------------------------------------------------- budgeting


def test_transient_below_limit_schedules_a_retry() -> None:
    outcome = _outcome(1, "transient")
    assert outcome.terminal is False
    assert outcome.retry_blocked is False
    assert outcome.next_retry_at == "2026-10-08T00:00:30Z"


def test_transient_at_limit_parks_without_a_retry_timestamp() -> None:
    outcome = _outcome(3, "transient")
    assert outcome.retry_blocked is True
    # Infrastructure failures deliberately never become terminal domain failures.
    assert outcome.terminal is False
    assert outcome.next_retry_at is None


def test_execution_at_limit_is_terminal_not_parked() -> None:
    outcome = _outcome(6, "execution")
    assert outcome.terminal is True
    assert outcome.retry_blocked is False
    assert outcome.next_retry_at is None


def test_execution_below_limit_retries_against_its_own_budget() -> None:
    outcome = _outcome(5, "execution")
    assert outcome.limit == _Config.execution_retry_limit
    assert outcome.terminal is False
    assert outcome.retry_blocked is False


def test_backoff_doubles_then_caps_at_retry_max() -> None:
    class Roomier(_Config):
        infrastructure_retry_limit = 99

    assert _outcome(1, "transient", Roomier()).next_retry_at == "2026-10-08T00:00:30Z"
    assert _outcome(2, "transient", Roomier()).next_retry_at == "2026-10-08T00:01:00Z"
    assert _outcome(3, "transient", Roomier()).next_retry_at == "2026-10-08T00:02:00Z"
    # 30 * 2**9 = 15360s, capped to retry_max_seconds.
    assert _outcome(10, "transient", Roomier()).next_retry_at == "2026-10-08T00:05:00Z"


# ------------------------------------------------------------------ integration


def _audit_events(client, task_id: str) -> list[dict]:
    response = client.get(f"/api/v1/audit-events?task_id={task_id}&limit=200")
    assert response.status_code == 200, response.text
    return response.json()


def _release(client, task_id: str, run_id: str, lease_id: str, failure_class="transient"):
    return client.post(
        f"/api/v1/dispatch/tasks/{task_id}/reason-runs/{run_id}/release",
        json={
            "lease_id": lease_id,
            "failure_kind": "worker_infrastructure",
            "failure_class": failure_class,
        },
    )


def test_blocked_reason_run_is_announced_narrowed_and_recoverable(client, engine) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=1)
    run_id = completed["next_reason_run_id"]
    claim, _input = claim_reason_run(client, task_id=task["id"], reason_run_id=run_id)
    lease_id = claim["lease_id"]

    # Give the run a two-entry pool and put it one failure away from its limit,
    # so this single release is the one that parks it.
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == run_id)
            .values(
                worker_pool_json='["worker-a", "worker-b"]',
                worker="worker-a",
                infrastructure_retry_count=2,
                status="running",
                lease_id=lease_id,
            )
        )

    released = _release(client, task["id"], run_id, lease_id)
    assert released.status_code == 200, released.text
    body = released.json()
    assert body["status"] == "pending"
    assert body["next_retry_at"] is None, "parked runs carry no retry timestamp"

    # 1. The parking is announced, not silent.
    blocked = [
        event for event in _audit_events(client, task["id"])
        if event["event_type"] == "reason_run.retry_blocked"
    ]
    assert blocked, "a parked ReasonRun must leave an explicit audit event"
    assert blocked[-1]["level"] == "warning"
    assert "需人工恢复" in blocked[-1]["message"]
    assert blocked[-1]["payload"]["limit"] == 3

    # 2. The failing worker left this run's pool, so the retry can land elsewhere.
    with engine.begin() as conn:
        pool = conn.execute(
            select(reason_runs.c.worker_pool_json).where(
                reason_runs.c.task_id == task["id"], reason_runs.c.id == run_id
            )
        ).scalar_one()
    assert "worker-a" not in pool
    assert "worker-b" in pool

    # 3. The Task surfaces it instead of looking merely idle.
    detail = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert detail["blocked_work_count"] >= 1

    # 4. An operator can release it again (the missing "can't be ended" path).
    reset = client.post(
        f"/api/v1/tasks/{task['id']}/reason-runs/{run_id}/reset-retries"
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["infrastructure_retry_count"] == 0

    assert any(
        event["event_type"] == "reason_run.retry_budget_reset"
        for event in _audit_events(client, task["id"])
    )
    after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert after["blocked_work_count"] == 0


def test_below_limit_release_records_no_blocked_event(client, engine) -> None:
    """A normal retry must not be announced as parked."""

    task, completed = _bootstrap_to_reason(client, reason_queue_limit=1)
    run_id = completed["next_reason_run_id"]
    claim, _input = claim_reason_run(client, task_id=task["id"], reason_run_id=run_id)

    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == run_id)
            .values(infrastructure_retry_count=0, status="running", lease_id=claim["lease_id"])
        )

    released = _release(client, task["id"], run_id, claim["lease_id"])
    assert released.status_code == 200, released.text
    assert released.json()["next_retry_at"] is not None

    assert not [
        event for event in _audit_events(client, task["id"])
        if event["event_type"] == "reason_run.retry_blocked"
    ]


def test_reset_retries_rejects_a_running_run(client) -> None:
    task, completed = _bootstrap_to_reason(client, reason_queue_limit=1)
    run_id = completed["next_reason_run_id"]
    claim_reason_run(client, task_id=task["id"], reason_run_id=run_id)

    response = client.post(f"/api/v1/tasks/{task['id']}/reason-runs/{run_id}/reset-retries")
    assert response.status_code == 409, response.text


def test_reset_retries_rejects_an_unknown_run(client) -> None:
    task, _completed = _bootstrap_to_reason(client, reason_queue_limit=1)
    response = client.post(f"/api/v1/tasks/{task['id']}/reason-runs/rr999999/reset-retries")
    assert response.status_code == 404, response.text
