from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from axiom.dispatcher.protocol.client import ApiResult
from axiom.dispatcher.runtime.cancellation import TaskCancellation
from axiom.dispatcher.runtime.process import ProcessResult
from axiom.dispatcher.tasks.common import HealthcheckRun, did_timeout
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


def _ensure_explore_artifact_files(project_id: str, intent_id: str) -> tuple[str, str]:
    ai_dir = Path(f"/tmp/axiom-files/unknown/{project_id}/ai")
    ai_dir.mkdir(parents=True, exist_ok=True)
    findings = ai_dir / f"{intent_id}_findings.md"
    leads = ai_dir / f"{intent_id}_leads.md"
    findings.write_text("# findings\n", encoding="utf-8")
    leads.write_text("# leads\n", encoding="utf-8")
    return str(findings), str(leads)


class SessionAwareFakeDriver(FakeDriver):
    def __init__(
        self,
        *,
        extracted_session: str | None,
        latest_session: str | None,
        valid_sessions: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._extracted_session = extracted_session
        self._latest_session = latest_session
        self._valid_sessions = valid_sessions or set()
        self.conclude_sessions: list[str] = []

    def extract_session(self, _session: str | None, _stdout: str, _stderr: str) -> str | None:
        return self._extracted_session

    def session_exists(self, _worker, session_id: str | None) -> bool:
        return bool(session_id and session_id in self._valid_sessions)

    def find_session_from_files(self, _worker_name: str) -> str | None:
        return self._latest_session

    def build_conclude(self, _worker, prompt: str, session: str, phase: str = "") -> list[str]:
        self.conclude_prompts.append(prompt)
        self.conclude_sessions.append(session)
        return ["conclude"]


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
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next step","targets":["api.example.com"],"timeouts":{"execute_seconds":120,"conclude_seconds":45}}]}}',
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
    assert client.created_intents == [
        ("proj_001", ["f001"], "next step", "test-worker", ["api.example.com"], {"execute_seconds": 120, "conclude_seconds": 45})
    ]
    assert client.released_reasons == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert graph_yaml not in driver.execute_prompts[0]
    # graph snapshot path uses real tmp path; just verify it contains the expected structure
    assert "graph.yaml" in driver.execute_prompts[0]
    assert "reason_execute-" in driver.execute_prompts[0]


def test_did_timeout_treats_sigkill_like_timeout() -> None:
    assert did_timeout(ProcessResult(-9, "", "", timed_out=False, cancelled=False))
    assert did_timeout(ProcessResult(-15, "", "", timed_out=False, cancelled=False))
    assert not did_timeout(ProcessResult(-9, "", "", timed_out=False, cancelled=True))


def test_explore_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()
    findings_path, leads_path = _ensure_explore_artifact_files(project.project.id, intent.id)
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"confirmed fact. evidence: '
                + findings_path
                + ' leads: '
                + leads_path
                + '"}}',
                "",
            ),
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
    assert client.concluded == [
        (
            "proj_001",
            "i001",
            "test-worker",
            f"confirmed fact. evidence: {findings_path} leads: {leads_path}",
        )
    ]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert lease.started and lease.stopped


def test_explore_requires_artifact_files_and_references(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    _ensure_explore_artifact_files(project.project.id, intent.id)
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"confirmed fact without references"}}',
                "",
            ),
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"confirmed fact without references"}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
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

    assert outcome == "parse_failure"


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


def test_explore_parse_failure_is_typed(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "not-json", ""),
            ProcessResult(0, "still-not-json", ""),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
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

    assert outcome == "parse_failure"
    assert client.released == [("proj_001", "i001", "test-worker")]


def test_explore_double_timeout_is_typed(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(137, "", "", timed_out=True),
            ProcessResult(137, "", "", timed_out=True),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
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

    assert outcome == "hard_timeout_both"
    assert client.released == [("proj_001", "i001", "test-worker")]


def test_explore_timeout_invalid_preallocated_uses_latest_valid_session(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    findings_path, leads_path = _ensure_explore_artifact_files(project.project.id, intent.id)
    driver = SessionAwareFakeDriver(
        extracted_session="session-preallocated-invalid",
        latest_session="session-latest-valid",
        valid_sessions={"session-latest-valid"},
    )
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(137, "", "", timed_out=True),
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"fallback fact. evidence: '
                + findings_path
                + ' leads: '
                + leads_path
                + '"}}',
                "",
            ),
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
    assert driver.conclude_sessions == ["session-latest-valid"]
    assert client.concluded


def test_explore_timeout_without_valid_session_keeps_failure_type(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    driver = SessionAwareFakeDriver(
        extracted_session="session-preallocated-invalid",
        latest_session=None,
        valid_sessions=set(),
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "run_healthcheck", _healthy)
    monkeypatch.setattr(
        explore,
        "_run_process",
        lambda *_args, **_kwargs: ProcessResult(137, "", "", timed_out=True),
    )

    outcome = explore.run_explore_task(
        config,
        client,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "hard_timeout_execute"
    assert driver.conclude_sessions == []


def test_explore_conclude_salvages_overlong_description(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    findings_path, leads_path = _ensure_explore_artifact_files(project.project.id, intent.id)
    long_body = "A" * 600
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(137, "", "", timed_out=True),
            ProcessResult(
                0,
                '{"accepted":true,"data":{"description":"'
                + long_body
                + " refs "
                + findings_path
                + " "
                + leads_path
                + '"}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
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
    assert client.concluded
    assert long_body in client.concluded[0][3]
    assert findings_path in client.concluded[0][3]
    assert leads_path in client.concluded[0][3]


def test_explore_conclude_salvages_description_from_non_json_text(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    findings_path, leads_path = _ensure_explore_artifact_files(project.project.id, intent.id)
    long_body = "B" * 600
    noisy_stdout = (
        'noise... "description":"'
        + long_body
        + " refs "
        + findings_path
        + " "
        + leads_path
        + '" trailing'
    )
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(137, "", "", timed_out=True),
            ProcessResult(0, noisy_stdout, ""),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda _name: FakeDriver())
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
    assert client.concluded
    assert long_body in client.concluded[0][3]
    assert findings_path in client.concluded[0][3]
    assert leads_path in client.concluded[0][3]


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


def test_bootstrap_timeout_invalid_preallocated_uses_latest_valid_session(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    driver = SessionAwareFakeDriver(
        extracted_session="session-preallocated-invalid",
        latest_session="session-latest-valid",
        valid_sessions={"session-latest-valid"},
    )
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(137, "", "", timed_out=True),
            ProcessResult(
                0,
                '{"accepted":true,"data":{"fact":{"description":"recovered from conclude"}}}',
                "",
            ),
        ]
    )

    monkeypatch.setattr(bootstrap, "get_driver", lambda _name: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(bootstrap, "run_healthcheck", _healthy)
    monkeypatch.setattr(bootstrap, "run_worker_process", lambda *_args, **_kwargs: next(results))

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert driver.conclude_sessions == ["session-latest-valid"]


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
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next","targets":["api.example.com"],"timeouts":{"execute_seconds":120,"conclude_seconds":45}}]}}',
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
    assert client.created_intents == [
        ("proj_001", ["f001"], "next", "test-worker", ["api.example.com"], {"execute_seconds": 120, "conclude_seconds": 45})
    ]
