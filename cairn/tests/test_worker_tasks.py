from __future__ import annotations

from collections.abc import Iterator

from axiom.dispatcher.protocol.client import ApiResult
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.process import ProcessResult
from axiom.dispatcher.tasks.common import HealthcheckRun
from axiom.dispatcher.tasks import bootstrap, explore, reason

from conftest import (
    FakeClient,
    FakeDriver,
    FakeLease,
    make_config,
    make_intent,
    make_project,
)


def _healthy(*_args, **_kwargs) -> HealthcheckRun:
    return HealthcheckRun(ProcessResult(0, "", ""), duration_ms=1)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


def test_reason_writes_graph_snapshot_and_creates_intent(monkeypatch, tmp_path) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()
    graph_yaml = "project:\n  title: huge\n" + ("x" * 100_000)

    monkeypatch.setattr(reason, "get_driver", lambda _name: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next step"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        project,
        graph_yaml,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next step", "test-worker")]
    assert client.released_reasons == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert graph_yaml not in driver.execute_prompts[0]
    # graph snapshot path uses real tmp path; just verify it contains the expected structure
    assert "graph.yaml" in driver.execute_prompts[0]
    assert "reason_execute-" in driver.execute_prompts[0]


def test_explore_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(0, '{"accepted":true,"data":{"description":"confirmed fact"}}', ""),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "run_healthcheck", _healthy)
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = explore.run_explore_task(
        config,
        client,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "confirmed fact")]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert lease.started and lease.stopped


def test_explore_healthcheck_failure_releases_claim(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_and_task"
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(
        explore,
        "run_healthcheck",
        lambda *_args, **_kwargs: HealthcheckRun(ProcessResult(1, "", "unhealthy"), duration_ms=1),
    )

    outcome = explore.run_explore_task(
        config,
        client,
        project,
        "graph",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "unhealthy"
    assert client.released == [("proj_001", "i001", "test-worker")]


def test_bootstrap_success_concludes_fact_then_completes_project(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(bootstrap, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        bootstrap,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"fact":{"description":"solved"},'
            '"complete":{"description":"goal met"}}}',
            "",
        ),
    )

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "solved")]
    assert client.completed == [("proj_001", ["f002"], "goal met", "test-worker")]
    assert lease.started and lease.stopped


def test_bootstrap_fact_only_writes_fact_without_completing_project(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(bootstrap, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        bootstrap,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"fact":{"description":"initial foothold"}}}',
            "",
        ),
    )

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "initial foothold")]
    assert client.completed == []


def test_reason_complete_treats_inactive_project_as_success(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    lease = FakeLease()

    def complete(*_args, **_kwargs) -> ApiResult:
        return ApiResult(403, text="inactive")

    client.complete = complete  # type: ignore[method-assign]
    monkeypatch.setattr(reason, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.released_reasons == [("proj_001", "test-worker")]


def test_reason_startup_only_mode_skips_task_healthcheck(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_only"
    project = make_project()
    client = FakeClient(project)
    lease = FakeLease()

    monkeypatch.setattr(reason, "get_driver", lambda _name: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_healthcheck",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("task healthcheck should be skipped")),
    )
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next", "test-worker")]
