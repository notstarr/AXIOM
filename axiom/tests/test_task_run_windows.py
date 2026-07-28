from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import update

from axiom.dispatcher.runner import AxiomRunner
from axiom.dispatcher.schemas import DispatcherSnapshot
from axiom.modules.tasks.tables import tasks
from axiom.shared.clock import parse_utc
from protocol_helpers import claim_reason_run, complete_bootstrap, open_reason_workspace


def _create_task(client, *, duration_seconds: int | None = None) -> dict:
    project = client.post("/api/v1/projects", json={"title": "定时任务"}).json()
    payload = {
        "project_id": project["id"],
        "title": "运行窗口测试",
        "origin": "a.example.com",
        "goal": "完成渗透测试",
        "worker_pool": ["worker-a"],
    }
    if duration_seconds is not None:
        payload["run_duration_seconds"] = duration_seconds
    response = client.post("/api/v1/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _expire(engine, task_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(run_deadline_at="2000-01-01T00:00:00Z")
        )


def test_create_task_supports_unlimited_and_timed_run_windows(client) -> None:
    unlimited = _create_task(client)
    assert unlimited["run_duration_seconds"] is None
    assert unlimited["run_started_at"]
    assert unlimited["run_deadline_at"] is None

    timed = _create_task(client, duration_seconds=7_200)
    assert timed["run_duration_seconds"] == 7_200
    assert (
        parse_utc(timed["run_deadline_at"]) - parse_utc(timed["run_started_at"])
    ).total_seconds() == 7_200

    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": timed["id"], "event_type": "task.run_window_started"},
    ).json()
    assert len(events) == 1
    assert events[0]["payload"]["source"] == "create"
    assert events[0]["payload"]["run_duration_seconds"] == 7_200


def test_task_run_duration_requires_whole_minutes_and_safe_range(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "校验"}).json()
    base = {
        "project_id": project["id"],
        "title": "非法时间",
        "origin": "a.example.com",
        "goal": "测试",
    }
    for invalid in (0, 90, 365 * 24 * 60 * 60 + 60):
        response = client.post(
            "/api/v1/tasks",
            json={**base, "run_duration_seconds": invalid},
        )
        assert response.status_code == 422


def test_resume_replaces_the_previous_window_and_keeps_legacy_no_body_compatible(client) -> None:
    task = _create_task(client, duration_seconds=7_200)
    stopped = client.post(f"/api/v1/tasks/{task['id']}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["last_pause_reason"] == "manual"

    resumed = client.post(
        f"/api/v1/tasks/{task['id']}/resume",
        json={"run_duration_seconds": 3_600},
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["run_duration_seconds"] == 3_600
    assert body["last_pause_reason"] is None
    assert (
        parse_utc(body["run_deadline_at"]) - parse_utc(body["run_started_at"])
    ).total_seconds() == 3_600

    client.post(f"/api/v1/tasks/{task['id']}/stop")
    unlimited = client.post(f"/api/v1/tasks/{task['id']}/resume")
    assert unlimited.status_code == 200
    assert unlimited.json()["run_duration_seconds"] is None
    assert unlimited.json()["run_deadline_at"] is None


def test_deadline_reconciler_pauses_active_intent_and_resume_restores_it(client, engine) -> None:
    task = _create_task(client, duration_seconds=3_600)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    )
    assert claim.status_code == 200
    _expire(engine, task["id"])

    reconciled = client.post("/api/v1/dispatch/task-deadlines/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["paused_task_ids"] == [task["id"]]

    stopped = client.get(f"/api/v1/tasks/{task['id']}").json()
    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert stopped["status"] == "stopped"
    assert stopped["phase"] == "none"
    assert stopped["last_pause_reason"] == "run_deadline_reached"
    assert intent["status"] == "cancelled"
    assert intent["last_failure_kind"] == "run_deadline_reached"
    assert intent["lease_id"] is None

    resumed = client.post(
        f"/api/v1/tasks/{task['id']}/resume",
        json={"run_duration_seconds": 600},
    )
    assert resumed.status_code == 200
    restored = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert restored["status"] == "pending"
    assert restored["last_failure_kind"] == ""


def test_deadline_reconciler_cancels_reason_run_and_workspace(client, engine) -> None:
    task = _create_task(client, duration_seconds=3_600)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason"], "max_running": 2},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": claim["lease_id"]}).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=claim["lease_id"],
    )
    reason_claim, _execution = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=reason_claim)
    _expire(engine, task["id"])

    assert client.post("/api/v1/dispatch/task-deadlines/reconcile").status_code == 200
    reason = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{completed['next_reason_run_id']}"
    ).json()
    workspace_after = client.get(f"/api/v1/reason-workspaces/{workspace['id']}").json()
    assert reason["status"] == "cancelled"
    assert reason["last_failure_kind"] == "run_deadline_reached"
    assert reason["lease_id"] is None
    assert workspace_after["status"] == "abandoned"
    assert workspace_after["failure_reason"] == "run_deadline_reached"


def test_deadline_reconciliation_is_idempotent_across_concurrent_runners(client, engine) -> None:
    task = _create_task(client, duration_seconds=60)
    _expire(engine, task["id"])
    service = client.app.state.container.tasks

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: service.reconcile_run_deadlines(), range(2)))

    assert sum(task["id"] in report.paused_task_ids for report in reports) == 1
    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": task["id"], "event_type": "task.paused"},
    ).json()
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "run_deadline_reached"


def test_expired_task_cannot_be_claimed_before_reconciliation(client, engine) -> None:
    task = _create_task(client, duration_seconds=60)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    _expire(engine, task["id"])
    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Task run deadline has elapsed"


def test_expired_task_does_not_trigger_initial_or_periodic_reason(client, engine) -> None:
    task = _create_task(client, duration_seconds=60)
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason"], "max_running": 2},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(f"{base}/start", json={"lease_id": claim["lease_id"]}).status_code == 200
    _expire(engine, task["id"])

    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=claim["lease_id"],
    )
    assert completed["next_reason_run_id"] is None
    after_bootstrap = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert after_bootstrap["phase"] == "waiting"
    assert after_bootstrap["active_reason_run"] is None

    reconciled = client.post("/api/v1/dispatch/reason-runs/reconcile").json()
    assert reconciled["created_reason_run_ids"] == []


def test_runner_reconciles_deadlines_before_reason_and_dispatch() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconcile_task_deadlines(self):
            self.calls.append("deadlines")

        def reconcile_reason_runs(self):
            self.calls.append("reason")

        def recover_stale(self, *, stale_after_seconds: int):
            assert stale_after_seconds > 0
            self.calls.append("recover")

        def snapshot(self) -> DispatcherSnapshot:
            self.calls.append("snapshot")
            return DispatcherSnapshot()

    control = ControlPlane()
    runner = AxiomRunner(control)
    try:
        runner.run_once()
    finally:
        runner.close()

    assert control.calls == ["deadlines", "reason", "recover", "snapshot"]
