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
