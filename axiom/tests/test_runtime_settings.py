from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.app import create_app
from axiom.dispatcher.http import HttpControlPlane
from axiom.dispatcher.runner import AxiomRunner
from protocol_helpers import complete_bootstrap, update_runtime_settings


class _ApiV1Client:
    def __init__(self, client: TestClient):
        self.client = client

    def request(self, method: str, path: str, json=None):
        return self.client.request(method, f"/api/v1{path}", json=json)


def test_runtime_settings_expose_the_formal_scheduler_defaults(client) -> None:
    config = client.get("/api/v1/settings/runtime").json()["config"]

    assert {
        key: config[key]
        for key in (
            "runner_interval_seconds",
            "global_max_active_runs",
            "global_max_active_tasks",
            "per_task_max_active_runs",
            "per_task_explore_concurrency",
            "heartbeat_interval_seconds",
            "lease_timeout_seconds",
            "bootstrap_execute_seconds",
            "bootstrap_conclude_seconds",
            "reason_execute_seconds",
            "reason_conclude_seconds",
            "reason_batch_size",
            "reason_low_watermark",
            "reason_max_wait_seconds",
            "reason_min_interval_seconds",
            "reason_target_open_intents",
            "reason_max_new_intents_per_run",
            "explore_execute_min_seconds",
            "explore_execute_max_seconds",
            "explore_conclude_min_seconds",
            "explore_conclude_max_seconds",
        )
    } == {
        "runner_interval_seconds": 3.0,
        "global_max_active_runs": 10,
        "global_max_active_tasks": 3,
        "per_task_max_active_runs": 4,
        "per_task_explore_concurrency": 4,
        "heartbeat_interval_seconds": 10,
        "lease_timeout_seconds": 60,
        "bootstrap_execute_seconds": 300,
        "bootstrap_conclude_seconds": 120,
        "reason_execute_seconds": 300,
        "reason_conclude_seconds": 120,
        "reason_batch_size": 2,
        "reason_low_watermark": 1,
        "reason_max_wait_seconds": 180,
        "reason_min_interval_seconds": 60,
        "reason_target_open_intents": 4,
        "reason_max_new_intents_per_run": 4,
        "explore_execute_min_seconds": 60,
        "explore_execute_max_seconds": 3600,
        "explore_conclude_min_seconds": 180,
        "explore_conclude_max_seconds": 600,
    }
    assert "max_parallel" not in config
    assert "bootstrap_concurrency" not in config
    assert "reason_concurrency" not in config


def test_runtime_settings_reject_explore_closeout_below_180_seconds(client) -> None:
    current = client.get("/api/v1/settings/runtime").json()
    response = client.put(
        "/api/v1/settings/runtime",
        json={
            "actor": "test",
            "expected_version": current["version"],
            "config": {
                **current["config"],
                "explore_conclude_min_seconds": 179,
            },
        },
    )

    assert response.status_code == 422


def test_runtime_settings_keep_zero_reject_stale_writes_and_survive_restart(
    engine,
    data_dir,
) -> None:
    with TestClient(create_app(engine=engine, data_dir=data_dir)) as first_client:
        initial = first_client.get("/api/v1/settings/runtime").json()
        config = {**initial["config"], "reason_low_watermark": 0}
        saved = first_client.put(
            "/api/v1/settings/runtime",
            json={
                "actor": "test",
                "expected_version": initial["version"],
                "config": config,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["config"]["reason_low_watermark"] == 0

        stale = first_client.put(
            "/api/v1/settings/runtime",
            json={
                "actor": "stale-editor",
                "expected_version": initial["version"],
                "config": config,
            },
        )
        assert stale.status_code == 409
        saved_version = saved.json()["version"]

    with TestClient(create_app(engine=engine, data_dir=data_dir)) as restarted_client:
        persisted = restarted_client.get("/api/v1/settings/runtime").json()
        assert persisted["version"] == saved_version
        assert persisted["config"]["reason_low_watermark"] == 0


def test_runner_acknowledges_the_runtime_version_it_actually_applied(client) -> None:
    saved = update_runtime_settings(
        client,
        runner_interval_seconds=1.5,
        global_max_active_runs=2,
        global_max_active_tasks=2,
        per_task_max_active_runs=2,
        per_task_explore_concurrency=2,
    )
    control = HttpControlPlane(
        "http://testserver",
        client=_ApiV1Client(client),
    )
    runner = AxiomRunner(control)
    try:
        report = runner.run_once()
    finally:
        runner.close()
        control.close()

    assert report.claimed == 0
    applied = client.get("/api/v1/settings/runtime").json()
    assert applied["applied_version"] == saved["version"]
    assert applied["applied_at"] is not None
    assert runner.runner_interval_seconds == 1.5
    assert runner.max_parallel == 2


def test_stage_budgets_are_snapshotted_at_claim_and_lower_limits_do_not_kill_work(
    client,
) -> None:
    update_runtime_settings(
        client,
        bootstrap_execute_seconds=600,
        bootstrap_conclude_seconds=150,
        reason_execute_seconds=500,
        reason_conclude_seconds=100,
    )
    project = client.post("/api/v1/projects", json={"title": "预算快照"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "预算快照",
            "origin": "a.example.com",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason"], "max_running": 1},
    )
    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    bootstrap_claim = client.post(
        f"{bootstrap_base}/claim",
        json={"worker_name": "worker-a"},
    ).json()

    update_runtime_settings(
        client,
        bootstrap_execute_seconds=900,
        bootstrap_conclude_seconds=180,
        reason_execute_seconds=700,
        reason_conclude_seconds=140,
    )
    assert client.post(
        f"{bootstrap_base}/start",
        json={"lease_id": bootstrap_claim["lease_id"]},
    ).status_code == 200
    bootstrap_input = client.post(
        f"{bootstrap_base}/input",
        json={"lease_id": bootstrap_claim["lease_id"]},
    ).json()
    assert bootstrap_input["execute_timeout_seconds"] == 600
    assert bootstrap_input["conclude_timeout_seconds"] == 150
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_claim["lease_id"],
    )

    reason_run_id = completed["next_reason_run_id"]
    reason_base = f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}"
    reason_claim = client.post(
        f"{reason_base}/claim",
        json={"worker_name": "worker-a"},
    )
    assert reason_claim.status_code == 200, reason_claim.text
    reason_lease = reason_claim.json()["lease_id"]

    update_runtime_settings(
        client,
        global_max_active_runs=1,
        global_max_active_tasks=1,
        per_task_max_active_runs=1,
        per_task_explore_concurrency=1,
        reason_execute_seconds=900,
        reason_conclude_seconds=180,
    )
    still_claimed = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}"
    ).json()
    assert still_claimed["status"] == "claimed"
    assert still_claimed["lease_id"] == reason_lease

    assert client.post(f"{reason_base}/start", json={"lease_id": reason_lease}).status_code == 200
    reason_input = client.post(f"{reason_base}/input", json={"lease_id": reason_lease}).json()
    assert reason_input["execute_timeout_seconds"] == 700
    assert reason_input["conclude_timeout_seconds"] == 140
