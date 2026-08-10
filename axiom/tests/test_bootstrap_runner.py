from __future__ import annotations

import threading
import time

import pytest

from axiom.dispatcher.bootstrap import (
    BootstrapExecutionTimeout,
    BootstrapRunner,
    TransientWorkerError,
)
from axiom.dispatcher.http import ControlPlaneError
from axiom.dispatcher.heartbeat import LeaseLostError
from axiom.dispatcher.loop import Dispatcher
from axiom.dispatcher.runner import AxiomRunner
from axiom.dispatcher.schemas import ClaimedIntent, DispatchReport, DispatcherSnapshot
from axiom.modules.execution.schemas import (
    BootstrapCompleted,
    CompleteBootstrapCommand,
    ExecutionInput,
    ReleaseIntentCommand,
)
from axiom.modules.workers.schemas import Worker
from protocol_helpers import write_bootstrap_files


VALID_OUTPUT = "FINISH"


class ApiControlPlane:
    def __init__(self, client, events: list[str]):
        self.client = client
        self.events = events

    def snapshot(self) -> DispatcherSnapshot:
        self.events.append("snapshot")
        response = self.client.get("/api/v1/dispatch/snapshot")
        assert response.status_code == 200, response.text
        return DispatcherSnapshot.model_validate(response.json())

    def worker(self, name: str) -> Worker:
        response = self.client.get(f"/api/v1/workers/{name}")
        assert response.status_code == 200, response.text
        return Worker.model_validate(response.json())

    def claim(self, *, task_id: str, intent_id: str, worker_name: str) -> ClaimedIntent | None:
        self.events.append("claim")
        response = self.client.post(
            f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
            json={"worker_name": worker_name},
        )
        if response.status_code == 409:
            return None
        assert response.status_code == 200, response.text
        return ClaimedIntent.model_validate(response.json())

    def start(self, work: ClaimedIntent) -> None:
        self.events.append("start")
        response = self.client.post(self._url(work, "start"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text

    def heartbeat(self, work: ClaimedIntent) -> None:
        self.events.append("heartbeat")
        response = self.client.post(
            self._url(work, "heartbeat"),
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 204, response.text

    def input(self, work: ClaimedIntent) -> ExecutionInput:
        self.events.append("input")
        response = self.client.post(self._url(work, "input"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text
        return ExecutionInput.model_validate(response.json())

    def begin_conclude(self, work: ClaimedIntent) -> None:
        self.events.append("begin_conclude")
        response = self.client.post(
            self._url(work, "conclude"),
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 204, response.text

    def complete_bootstrap(
        self,
        work: ClaimedIntent,
        command: CompleteBootstrapCommand,
    ) -> BootstrapCompleted:
        self.events.append("complete")
        response = self.client.post(
            self._url(work, "bootstrap-complete"),
            json=command.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        return BootstrapCompleted.model_validate(response.json())

    def release(self, work: ClaimedIntent, command: ReleaseIntentCommand) -> None:
        self.events.append("release")
        response = self.client.post(
            self._url(work, "release"),
            json=command.model_dump(mode="json"),
        )
        assert response.status_code in {200, 409}, response.text

    @staticmethod
    def _url(work: ClaimedIntent, action: str) -> str:
        return (
            f"/api/v1/dispatch/tasks/{work.task_id}/intents/"
            f"{work.intent_id}/{action}"
        )


class FakeBootstrapExecutor:
    def __init__(self, events: list[str], *, execute_result=VALID_OUTPUT, conclude_result=VALID_OUTPUT):
        self.events = events
        self.execute_result = execute_result
        self.conclude_result = conclude_result
        self.execute_calls = 0
        self.conclude_calls = 0
        self.close_calls = 0

    def execute(self, execution_input, *, lease_active):
        self.events.append("execute")
        self.execute_calls += 1
        assert lease_active()
        if isinstance(self.execute_result, BaseException):
            raise self.execute_result
        if self.execute_result == VALID_OUTPUT:
            write_bootstrap_files(execution_input.run_dir)
        return self.execute_result

    def conclude(self, execution_input, *, reason, lease_active):
        self.events.append(f"conclude:{reason}")
        self.conclude_calls += 1
        assert lease_active()
        if isinstance(self.conclude_result, BaseException):
            raise self.conclude_result
        if self.conclude_result == VALID_OUTPUT:
            write_bootstrap_files(execution_input.run_dir)
        return self.conclude_result

    def close(self):
        self.close_calls += 1


class BlockingBootstrapExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.closed = threading.Event()

    def execute(self, _execution_input, *, lease_active):
        self.started.set()
        while lease_active():
            time.sleep(0.01)
        self.cancelled.set()
        raise LeaseLostError("task is no longer active")

    def conclude(self, _execution_input, *, reason, lease_active):
        raise AssertionError(f"cancelled execution must not conclude: {reason}/{lease_active()}")

    def close(self):
        self.closed.set()


def claimed_work(client, events: list[str]) -> tuple[dict, ApiControlPlane, ClaimedIntent]:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "攻击面发现",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    control = ApiControlPlane(client, events)
    report = Dispatcher(control).tick()
    assert report.claimed == 1
    return task, control, report.claims[0]


def test_bootstrap_runner_executes_once_and_heartbeats_before_loading_context(client) -> None:
    events: list[str] = []
    task, control, work = claimed_work(client, events)
    executor = FakeBootstrapExecutor(events)

    result = BootstrapRunner(
        control,
        heartbeat_interval_seconds=60,
    ).run(work, executor)

    assert result.status == "completed"
    assert events == [
        "snapshot",
        "claim",
        "start",
        "heartbeat",
        "input",
        "execute",
        "complete",
    ]
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"


@pytest.mark.parametrize(
    ("execute_result", "reason"),
    [
        ("invalid", "最终响应必须仅包含 FINISH"),
        (BootstrapExecutionTimeout("budget exhausted"), "execute_timeout"),
    ],
)
def test_bootstrap_runner_uses_exactly_one_conclude_without_reexecuting(
    client,
    execute_result,
    reason,
) -> None:
    events: list[str] = []
    _task, control, work = claimed_work(client, events)
    executor = FakeBootstrapExecutor(events, execute_result=execute_result)

    BootstrapRunner(control, heartbeat_interval_seconds=60).run(work, executor)

    assert executor.execute_calls == 1
    assert executor.conclude_calls == 1
    assert f"conclude:{reason}" in events


def test_invalid_conclude_releases_intent_into_existing_retry_state(client) -> None:
    events: list[str] = []
    task, control, work = claimed_work(client, events)
    executor = FakeBootstrapExecutor(
        events,
        execute_result="invalid",
        conclude_result="invalid",
    )

    with pytest.raises(ValueError, match="FINISH"):
        BootstrapRunner(control, heartbeat_interval_seconds=60).run(work, executor)

    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert intent["status"] == "pending"
    assert intent["retry_count"] == 1
    assert intent["last_failure_kind"] == "bootstrap_execution_failed"
    assert intent["next_retry_at"] is not None
    assert executor.conclude_calls == 3
    assert events.count("begin_conclude") == 1
    assert events.count("release") == 1


def test_transient_control_plane_failure_does_not_consume_bootstrap_retry(client) -> None:
    events: list[str] = []
    task, control, work = claimed_work(client, events)
    executor = FakeBootstrapExecutor(events)

    def unavailable_input(_work: ClaimedIntent) -> ExecutionInput:
        events.append("input_timeout")
        raise ControlPlaneError("timed out", status_code=0)

    control.input = unavailable_input  # type: ignore[method-assign]

    with pytest.raises(ControlPlaneError, match="timed out"):
        BootstrapRunner(control, heartbeat_interval_seconds=60).run(work, executor)

    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert executor.execute_calls == 0
    assert executor.conclude_calls == 0
    assert intent["retry_count"] == 0
    assert intent["infrastructure_retry_count"] == 1
    assert intent["last_failure_kind"] == "control_plane_unavailable"
    assert intent["failure_class"] == "transient"


def test_transient_worker_failure_does_not_consume_execution_retry(client) -> None:
    events: list[str] = []
    task, control, work = claimed_work(client, events)
    executor = FakeBootstrapExecutor(
        events,
        execute_result=TransientWorkerError("provider disconnected"),
    )

    with pytest.raises(TransientWorkerError):
        BootstrapRunner(control, heartbeat_interval_seconds=60).run(work, executor)

    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert intent["retry_count"] == 0
    assert intent["infrastructure_retry_count"] == 1
    assert intent["last_failure_kind"] == "worker_infrastructure"


def test_axiom_runner_reconciles_and_executes_claims_without_blocking_the_tick(client) -> None:
    events: list[str] = []
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "攻击面发现",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={
            "capabilities": ["bootstrap"],
            "max_running": 1,
            "config": {"model": "fake"},
        },
    )
    control = ApiControlPlane(client, events)
    executor = FakeBootstrapExecutor(events)
    runner = AxiomRunner(
        control,
        executor_factory=lambda _worker, _work: executor,
        heartbeat_interval_seconds=60,
    )
    try:
        report = runner.run_once()
        assert report.claimed == 1
        runner.wait_for_idle()
    finally:
        runner.close()

    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "reason"
    assert client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0][
        "status"
    ] == "completed"
    assert executor.close_calls == 1


def test_axiom_runner_closes_executor_after_execution_failure(client) -> None:
    events: list[str] = []
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "执行异常清理",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    executor = FakeBootstrapExecutor(
        events,
        execute_result=RuntimeError("worker exploded"),
    )
    runner = AxiomRunner(
        ApiControlPlane(client, events),
        executor_factory=lambda _worker, _work: executor,
        heartbeat_interval_seconds=60,
    )
    try:
        assert runner.run_once().claimed == 1
        runner.wait_for_idle()
    finally:
        runner.close()

    assert executor.close_calls == 1
    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert intent["last_failure_kind"] == "bootstrap_execution_failed"


def test_axiom_runner_cancels_local_execution_when_task_stops(client) -> None:
    events: list[str] = []
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "停止运行中的任务",
            "origin": "a.example.com",
            "goal": "完成渗透测试",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    executor = BlockingBootstrapExecutor()
    runner = AxiomRunner(
        ApiControlPlane(client, events),
        executor_factory=lambda _worker, _work: executor,
        max_parallel=1,
        heartbeat_interval_seconds=60,
    )
    try:
        assert runner.run_once().claimed == 1
        assert executor.started.wait(2)
        assert client.post(f"/api/v1/tasks/{task['id']}/stop").status_code == 200

        runner.run_once()

        assert executor.cancelled.wait(2)
        runner.wait_for_idle(timeout_seconds=2)
        assert executor.closed.wait(2)
    finally:
        runner.close()

    current = client.get(f"/api/v1/tasks/{task['id']}").json()
    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert current["status"] == "stopped"
    assert intent["status"] == "cancelled"
    assert intent["last_failure_kind"] == "task_stopped"
    assert intent["worker"] is None


def test_axiom_runner_keeps_running_when_control_plane_is_temporarily_unavailable() -> None:
    runner = AxiomRunner(object())
    stop_event = threading.Event()
    calls = 0

    def run_once() -> DispatchReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ControlPlaneError("connection refused", status_code=0)
        stop_event.set()
        return DispatchReport()

    runner.run_once = run_once  # type: ignore[method-assign]
    try:
        runner.run_forever(stop_event=stop_event, interval_seconds=0.001)
    finally:
        runner.close()

    assert calls == 2
