from __future__ import annotations

import threading
import time
from pathlib import Path

from sqlalchemy import select

from axiom.dispatcher.runner import AxiomRunner
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from axiom.modules.graph.tables import intent_sources
from protocol_helpers import complete_bootstrap, open_reason_workspace, update_runtime_settings


class _ApiControlPlane:
    def __init__(self, client) -> None:
        self.client = client

    def snapshot(self) -> DispatcherSnapshot:
        response = self.client.get("/api/v1/dispatch/snapshot")
        assert response.status_code == 200, response.text
        return DispatcherSnapshot.model_validate(response.json())

    def worker(self, name: str):
        from axiom.modules.workers.schemas import Worker

        response = self.client.get(f"/api/v1/workers/{name}")
        assert response.status_code == 200, response.text
        return Worker.model_validate(response.json())

    def claim(self, *, task_id: str, intent_id: str, worker_name: str):
        response = self.client.post(
            f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
            json={"worker_name": worker_name},
        )
        if response.status_code == 409:
            return None
        assert response.status_code == 200, response.text
        return ClaimedIntent.model_validate(response.json())

    def start(self, work: ClaimedIntent) -> None:
        response = self.client.post(self._url(work, "start"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text

    def heartbeat(self, work: ClaimedIntent) -> None:
        response = self.client.post(
            self._url(work, "heartbeat"), json={"lease_id": work.lease_id}
        )
        assert response.status_code == 204, response.text

    def input(self, work: ClaimedIntent):
        from axiom.modules.execution.schemas import ExecutionInput

        response = self.client.post(self._url(work, "input"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text
        return ExecutionInput.model_validate(response.json())

    def begin_conclude(self, work: ClaimedIntent) -> None:
        response = self.client.post(
            self._url(work, "conclude"), json={"lease_id": work.lease_id}
        )
        assert response.status_code == 204, response.text

    def complete_explore(self, work: ClaimedIntent, command):
        from axiom.modules.execution.schemas import ExploreCompleted

        response = self.client.post(
            self._url(work, "explore-complete"),
            json=command.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        return ExploreCompleted.model_validate(response.json())

    def release(self, work: ClaimedIntent, command) -> None:
        response = self.client.post(
            self._url(work, "release"), json=command.model_dump(mode="json")
        )
        assert response.status_code in {200, 409}, response.text

    def expire_interventions(self, _work: ClaimedIntent) -> None:
        return None

    @staticmethod
    def _url(work: ClaimedIntent, action: str) -> str:
        return (
            f"/api/v1/dispatch/tasks/{work.task_id}/intents/"
            f"{work.intent_id}/{action}"
        )


class _FanoutExploreExecutor:
    """Small deterministic Explore executor for the 20-intent acceptance flow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started_ids: list[str] = []
        self.finished_ids: list[str] = []
        self.first_returned_at: float | None = None
        self.fifth_started = threading.Event()
        self.four_active = threading.Event()
        self.release_first = threading.Event()
        self.release_rest = threading.Event()

    def execute(self, execution_input, *, lease_active):
        intent_id = execution_input.intent_id
        assert intent_id is not None
        with self._lock:
            self.started_ids.append(intent_id)
            first = len(self.started_ids) == 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                self.four_active.set()
            if len(self.started_ids) >= 5:
                self.fifth_started.set()

        # Hold the first four executions long enough to observe the capacity
        # boundary.  Releasing one proves that the runner fills the freed slot
        # while the other three executions are still active.
        (self.release_first if first else self.release_rest).wait(10)
        assert lease_active()

        artifacts = Path(execution_input.run_dir) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "findings.md").write_text(
            f"# Findings\n\n- completed {intent_id}\n", encoding="utf-8"
        )
        (artifacts / "leads.md").write_text(
            "# Leads\n\n- no additional lead\n", encoding="utf-8"
        )
        with self._lock:
            self.finished_ids.append(intent_id)
            if first:
                self.first_returned_at = time.monotonic()
            self.active -= 1
        return "FINISH"

    def conclude(self, execution_input, *, reason, lease_active):
        raise AssertionError(f"unexpected conclude: {execution_input.intent_id}/{reason}")

    def close(self) -> None:
        return None


def _create_fanout_task(client) -> tuple[dict, str, list[str]]:
    update_runtime_settings(
        client,
        global_max_active_runs=4,
        global_max_active_tasks=1,
        per_task_max_active_runs=4,
        per_task_explore_concurrency=4,
        per_task_review_concurrency=4,
        # Keep the acceptance run in Explore while its 20 queued Intents drain.
        reason_min_interval_seconds=3_600,
        reason_batch_size=20,
        reason_low_watermark=0,
        reason_max_wait_seconds=3_600,
    )
    project = client.post("/api/v1/projects", json={"title": "动态 Todo 验收"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "20 Intent fan-out",
            "origin": "fanout.example.com",
            "goal": "验证 20 个攻击面方向",
            "worker_pool": ["worker-a"],
        },
    ).json()
    worker = client.put(
        "/api/v1/workers/worker-a",
        json={
            "capabilities": ["bootstrap", "reason", "explore"],
            "max_running": 4,
        },
    )
    assert worker.status_code == 200, worker.text

    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    bootstrap_claim_response = client.post(
        f"{bootstrap_base}/claim", json={"worker_name": "worker-a"}
    )
    assert bootstrap_claim_response.status_code == 200, bootstrap_claim_response.text
    bootstrap_claim = bootstrap_claim_response.json()
    assert client.post(
        f"{bootstrap_base}/start", json={"lease_id": bootstrap_claim["lease_id"]}
    ).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_claim["lease_id"],
    )
    fact_id = completed["fact_id"]

    reason_run_id = completed["next_reason_run_id"]
    assert reason_run_id
    reason_claim_response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}/claim",
        json={"worker_name": "worker-a"},
    )
    assert reason_claim_response.status_code == 200, reason_claim_response.text
    reason_claim = reason_claim_response.json()
    reason_base = f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}"
    assert client.post(
        f"{reason_base}/start", json={"lease_id": reason_claim["lease_id"]}
    ).status_code == 200
    reason_input = client.post(
        f"{reason_base}/input", json={"lease_id": reason_claim["lease_id"]}
    ).json()
    workspace = open_reason_workspace(client, task_id=task["id"], claim={**reason_claim, **reason_input})

    todo_response = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/todos",
        json={
            "lease_id": reason_claim["lease_id"],
            "todo": {
                "ref": "todo-fanout",
                "title": "验证 fan-out 攻击面",
                "category": "authorization",
                "sources": [{"type": "fact", "id": fact_id}],
            },
        },
    )
    assert todo_response.status_code == 200, todo_response.text

    intent_ids: list[str] = []
    for number in range(20):
        response = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/intents",
            json={
                "lease_id": reason_claim["lease_id"],
                "intent": {
                    "todo_ref": "todo-fanout",
                    "from_facts": [fact_id],
                    "description": f"验证 fan-out 方向 {number + 1}",
                    "targets": ["fanout.example.com"],
                    "priority": number,
                    # Explore budgets are bounded by the runtime minimum.
                    "execute_seconds": 300,
                    "conclude_seconds": 180,
                },
            },
        )
        assert response.status_code == 200, response.text

    assessment = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/goal-assessment",
        json={
            "lease_id": reason_claim["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "20 个攻击面方向尚待 Explore 验证",
                "evidence_refs": [f"fact:{fact_id}"],
            },
        },
    )
    assert assessment.status_code == 200, assessment.text
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": reason_claim["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    intent_ids = committed.json()["created_intent_ids"]
    assert len(intent_ids) == 20
    return task, fact_id, intent_ids


def test_twenty_pending_intents_are_persisted_and_refilled_at_four_slots(client, engine) -> None:
    task, fact_id, intent_ids = _create_fanout_task(client)
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    explores = [item for item in graph["intents"] if item["kind"] == "explore"]
    assert len(explores) == 20
    todos = client.get(f"/api/v1/tasks/{task['id']}/todos").json()
    assert len(todos) == 1
    assert {item["todo_id"] for item in explores} == {todos[0]["id"]}
    assert {fact_id} == {fact for item in explores for fact in item["from_facts"]}
    with engine.connect() as conn:
        source_rows = conn.execute(
            select(intent_sources.c.intent_id, intent_sources.c.fact_id).where(
                intent_sources.c.task_id == task["id"],
                intent_sources.c.intent_id.in_(intent_ids),
            )
        ).all()
    assert len(source_rows) == 20
    assert {(intent_id, fact_id) for intent_id, fact_id in source_rows} == {
        (intent_id, fact_id) for intent_id in intent_ids
    }

    executor = _FanoutExploreExecutor()
    control = _ApiControlPlane(client)
    runner = AxiomRunner(
        control,
        executor_factory=lambda _worker, _work: executor,
        max_parallel=4,
        heartbeat_interval_seconds=60,
    )
    stop_event = threading.Event()
    loop = threading.Thread(
        target=runner.run_forever,
        kwargs={"stop_event": stop_event, "interval_seconds": 30.0},
        daemon=True,
    )
    loop.start()
    try:
        assert executor.four_active.wait(5), "runner did not start four Explore Intents"
        with executor._lock:
            assert executor.active == 4
        executor.release_first.set()
        assert executor.fifth_started.wait(5), (
            "runner did not refill a freed slot without another Reason run"
        )
        with executor._lock:
            assert executor.active == 4
            assert executor.max_active <= 4
        assert executor.first_returned_at is not None

        executor.release_rest.set()
        runner.wait_for_idle(timeout_seconds=20)
    finally:
        stop_event.set()
        runner._dispatch_wakeup.set()
        loop.join(timeout=5)
        runner.close()

    assert not loop.is_alive()
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    by_id = {item["id"]: item for item in graph["intents"]}
    assert set(intent_ids) <= set(by_id)
    assert all(by_id[item_id]["status"] == "completed" for item_id in intent_ids)
    assert len(executor.finished_ids) == 20
    assert executor.max_active <= 4
