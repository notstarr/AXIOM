"""Worker circuit breaker (``dispatch.breaker``).

``workers.cooldown_until`` existed before this, but as a bare timestamp written
with one fixed delay and never reset on success.  That meant a backend which kept
failing was re-tried at the same gentle interval forever, and there was no way to
tell "recovered" from "never tried again".

The tests pin the ladder, the decay that replaces a success callback, and the two
properties that keep it from becoming a trap: escalation is capped, and an
operator can clear it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from axiom.modules.dispatch import breaker
from axiom.modules.workers.tables import workers


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class _Config:
    worker_failure_cooldown_seconds = 30
    worker_failure_cooling_factor = 5
    worker_failure_cooldown_max_seconds = 1800
    worker_failure_trip_after = 2
    worker_breaker_decay_seconds = 3600


def _now(delta_seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _worker(client, name: str = "breaker-w") -> str:
    response = client.put(
        f"/api/v1/workers/{name}",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 2},
    )
    assert response.status_code == 200, response.text
    return name


def _row(engine, name: str):
    with engine.connect() as conn:
        return conn.execute(
            select(workers).where(workers.c.name == name)
        ).mappings().one()


def _trips(engine, client, name: str, count: int, *, at: int = 0, config=_Config()):
    with engine.begin() as conn:
        for step in range(count):
            breaker.trip(
                conn,
                worker_name=name,
                failure_kind="worker_infrastructure",
                now=_now(at + step),
                config=config,
            )


def test_the_cooldown_ladder_escalates_then_stops_growing() -> None:
    config = _Config()

    assert [breaker.cooldown_for_trip(trip, config) for trip in (1, 2, 3)] == [30, 150, 750]
    # Capped: after a few trips the point is "stop hammering this", not "exile it".
    assert breaker.cooldown_for_trip(4, config) == 1800
    assert breaker.cooldown_for_trip(50, config) == 1800


def test_a_single_failure_cools_without_escalating(client, engine) -> None:
    name = _worker(client)

    _trips(engine, client, name, 1)

    row = _row(engine, name)
    assert row["transient_failure_count"] == 1
    assert row["trip_count"] == 0
    assert row["cooldown_until"] == _now(30)


def test_repeated_failures_escalate_and_reset_the_streak(client, engine) -> None:
    name = _worker(client)

    _trips(engine, client, name, 2)

    row = _row(engine, name)
    assert row["trip_count"] == 1
    assert row["transient_failure_count"] == 0  # the streak became a trip
    assert row["cooldown_until"] == _now(1 + 30)

    _trips(engine, client, name, 2, at=60)

    row = _row(engine, name)
    assert row["trip_count"] == 2
    assert row["cooldown_until"] == _now(61 + 150)


def test_a_quiet_backend_gets_a_clean_slate(client, engine) -> None:
    """This is what replaces a success callback: silence means recovery."""

    name = _worker(client)
    _trips(engine, client, name, 4)
    assert _row(engine, name)["trip_count"] >= 1

    quiet = _Config.worker_breaker_decay_seconds + 60
    _trips(engine, client, name, 1, at=quiet)

    row = _row(engine, name)
    assert row["trip_count"] == 0
    assert row["transient_failure_count"] == 1
    # The first trip after a decay must cool briefly, not at the old ceiling.
    assert row["cooldown_until"] == _now(quiet + 30)


def test_failures_inside_the_decay_window_keep_escalating(client, engine) -> None:
    name = _worker(client)
    _trips(engine, client, name, 2)

    _trips(engine, client, name, 2, at=60)
    trip_before = _row(engine, name)["trip_count"]

    _trips(engine, client, name, 2, at=120)
    assert _row(engine, name)["trip_count"] == trip_before + 1


def test_reset_clears_the_whole_breaker(client, engine) -> None:
    name = _worker(client)
    _trips(engine, client, name, 4)
    assert _row(engine, name)["cooldown_until"] is not None

    response = client.post(f"/api/v1/workers/{name}/breaker/reset")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cooldown_until"] is None
    assert payload["trip_count"] == 0
    assert payload["transient_failure_count"] == 0
    assert payload["last_failure_kind"] == ""


def test_tripping_an_unknown_worker_is_a_no_op(engine) -> None:
    """Release paths call this for whatever worker failed; a deleted Worker must
    not turn a release into an exception."""

    with engine.begin() as conn:
        breaker.trip(
            conn,
            worker_name="does-not-exist",
            failure_kind="worker_infrastructure",
            now=_now(),
            config=_Config(),
        )


def test_an_empty_worker_name_is_ignored(engine) -> None:
    with engine.begin() as conn:
        breaker.trip(
            conn, worker_name="", failure_kind="x", now=_now(), config=_Config()
        )


def test_the_breaker_never_shortens_an_operator_set_cooldown(client, engine) -> None:
    """A human-set cooldown is a decision; a failure must not undo it early."""

    name = _worker(client)
    client.post(
        f"/api/v1/workers/{name}/cooldown",
        json={"cooldown_until": _now(3600)},
    )

    _trips(engine, client, name, 1)

    row = _row(engine, name)
    assert row["cooldown_until"] == _now(3600)
    assert row["last_failure_kind"] == "worker_infrastructure"


# ------------------------------------- platform faults are not worker faults


def test_is_platform_failure_recognises_our_own_control_plane() -> None:
    from axiom.modules.dispatch.retry import is_platform_failure

    assert is_platform_failure("control_plane_unavailable")
    assert is_platform_failure("runner_dispatch_failed")
    assert not is_platform_failure("worker_infrastructure")
    assert not is_platform_failure("")
    assert not is_platform_failure(None)


def test_blames_worker_only_for_faults_the_backend_produced() -> None:
    from axiom.modules.dispatch.retry import blames_worker

    # A backend that died: retried *and* charged to the worker.
    assert blames_worker("transient", "worker_infrastructure")
    # Our own API returned 500: retried, but the worker did nothing wrong.
    assert not blames_worker("transient", "control_plane_unavailable")
    assert not blames_worker("transient", "runner_dispatch_failed")
    # An execution failure already has its own budget; never trip the breaker.
    assert not blames_worker("execution", "worker_infrastructure")


def test_releasing_on_a_control_plane_failure_does_not_trip_the_worker(
    client, engine
) -> None:
    """A disk-full 500 must not exile the only healthy worker.

    Live incident: the API returned 500 for every work input because the disk was
    full.  The Runner correctly classified those as ``control_plane_unavailable``,
    but because the class is ``transient`` the release path charged them to the
    worker anyway — 26 trips — and narrowed the pool away from it.  With one
    capable backend that parked the whole Task while every audit record blamed a
    worker that had not even started.
    """

    name = _worker(client)
    project = client.post("/api/v1/projects", json={"title": "平台故障"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "平台故障任务",
            "origin": "cp.example.com",
            "worker_pool": [name],
        },
    ).json()

    from axiom.modules.reason.tables import reason_runs

    with engine.begin() as conn:
        conn.execute(
            reason_runs.insert().values(
                id="rr000902",
                task_id=task["id"],
                status="claimed",
                trigger="fact_batch",
                trigger_payload_json="{}",
                input_fingerprint="",
                worker_pool_json=f'["{name}"]',
                settings_version=1,
                execute_timeout_seconds=600,
                conclude_timeout_seconds=300,
                explore_execute_min_seconds=300,
                explore_execute_max_seconds=3600,
                explore_conclude_min_seconds=180,
                explore_conclude_max_seconds=600,
                retry_count=0,
                infrastructure_retry_count=0,
                next_retry_at=None,
                worker=name,
                lease_id="lease_cp",
                created_at="2026-10-26T00:00:00Z",
                updated_at="2026-10-26T00:00:00Z",
            )
        )

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/rr000902/release",
        json={
            "lease_id": "lease_cp",
            "failure_kind": "control_plane_unavailable",
            "failure_class": "transient",
        },
    )
    assert response.status_code == 200, response.text

    row = _row(engine, name)
    assert row["cooldown_until"] is None, (
        "a control-plane outage must not put the worker in cooldown"
    )
    assert int(row["trip_count"] or 0) == 0

    with engine.connect() as conn:
        pool = conn.execute(
            select(reason_runs.c.worker_pool_json).where(
                reason_runs.c.id == "rr000902"
            )
        ).scalar_one()
    assert json.loads(pool) == [name], (
        "the worker pool must survive a fault that never reached the worker"
    )
