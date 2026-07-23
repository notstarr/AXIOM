from __future__ import annotations

from sqlalchemy import update

from axiom.modules.graph.tables import intents
from axiom.dispatcher.runner import AxiomRunner
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from axiom.modules.execution.schemas import ExecutionInput
from axiom.modules.reason.schemas import CommitResult, ReasonWorkspace
from axiom.modules.workers.schemas import Worker


BOOTSTRAP = {
    "summary": "完成初始攻击面发现。",
    "artifacts": {
        "asset_inventory": "# 资产\n\n- a.example.com",
        "initial_leads": "# 线索\n\n- 验证对象级授权",
        "findings": "# 已确认问题\n\n- 本轮无确认漏洞",
    },
}


class ApiControlPlane:
    def __init__(self, client):
        self.client = client

    def snapshot(self) -> DispatcherSnapshot:
        return DispatcherSnapshot.model_validate(
            self.client.get("/api/v1/dispatch/snapshot").json()
        )

    def worker(self, name: str) -> Worker:
        return Worker.model_validate(self.client.get(f"/api/v1/workers/{name}").json())

    def claim(self, *, task_id: str, intent_id: str, worker_name: str) -> ClaimedIntent | None:
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
            self._url(work, "heartbeat"),
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 204, response.text

    def input(self, work: ClaimedIntent) -> ExecutionInput:
        response = self.client.post(self._url(work, "input"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text
        return ExecutionInput.model_validate(response.json())

    def open_reason_workspace(self, work: ClaimedIntent, *, context_revision: int):
        response = self.client.post(
            f"/api/v1/tasks/{work.task_id}/reason/workspaces",
            json={
                "intent_id": work.intent_id,
                "lease_id": work.lease_id,
                "context_revision": context_revision,
            },
        )
        assert response.status_code == 200, response.text
        return ReasonWorkspace.model_validate(response.json())

    def submit_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        workspace_id: str,
        draft,
    ) -> CommitResult:
        response = self.client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/submit",
            json={"lease_id": work.lease_id, "draft": draft.model_dump(mode="json")},
        )
        assert response.status_code == 200, response.text
        return CommitResult.model_validate(response.json())

    def abandon_reason_workspace(
        self,
        work: ClaimedIntent,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        response = self.client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/abandon",
            json={"lease_id": work.lease_id, "failure_reason": failure_reason},
        )
        assert response.status_code == 200, response.text

    def release(self, work: ClaimedIntent, command) -> None:
        response = self.client.post(
            self._url(work, "release"),
            json=command.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _url(work: ClaimedIntent, action: str) -> str:
        return f"/api/v1/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/{action}"


class FakeReasonExecutor:
    def __init__(self, outputs):
        self.outputs = outputs

    def execute(self, _execution_input, *, lease_active):
        assert lease_active()
        return self.outputs.pop(0)


def _pending_reason(client) -> tuple[dict, ApiControlPlane]:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Reason runner",
            "origin": "a.example.com",
            "goal": "覆盖攻击面并验证高价值方向",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 1},
    )
    bootstrap = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{base}/start", json={"lease_id": bootstrap["lease_id"]})
    client.post(
        f"{base}/bootstrap-complete",
        json={"lease_id": bootstrap["lease_id"], **BOOTSTRAP},
    )
    return task, ApiControlPlane(client)


def _valid_plan(fact_id: str = "f001") -> dict:
    return {
        "todo_adds": [
            {
                "ref": "authz",
                "title": "验证对象级授权",
                "category": "authorization",
                "theme": "IDOR",
                "priority": 90,
                "sources": [{"type": "fact", "id": fact_id}],
            }
        ],
        "todo_updates": [],
        "intent_proposals": [
            {
                "todo_ref": "authz",
                "from_facts": [fact_id],
                "description": "验证订单详情接口是否存在对象级授权问题",
                "targets": ["a.example.com"],
                "execute_seconds": 420,
                "conclude_seconds": 90,
            }
        ],
    }


def test_reason_runner_commits_a_plan_without_reason_choosing_a_worker(client) -> None:
    task, control = _pending_reason(client)
    outputs = [_valid_plan()]
    runner = AxiomRunner(
        control,
        executor_factory=lambda _worker, _work: FakeReasonExecutor(outputs),
        heartbeat_interval_seconds=60,
    )
    try:
        report = runner.run_once()
        runner.wait_for_idle()
    finally:
        runner.close()

    assert report.claimed == 1
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert next(item for item in graph["intents"] if item["id"] == "i002")["status"] == "completed"
    explore = next(item for item in graph["intents"] if item["kind"] == "explore")
    assert explore["status"] == "pending"


def test_invalid_reason_plan_abandons_workspace_and_can_retry_with_a_new_lease(client) -> None:
    task, control = _pending_reason(client)
    outputs = [{"todo_adds": [{"ref": "broken"}]}, _valid_plan()]
    runner = AxiomRunner(
        control,
        executor_factory=lambda _worker, _work: FakeReasonExecutor(outputs),
        heartbeat_interval_seconds=60,
    )
    try:
        first = runner.run_once()
        runner.wait_for_idle()
        with client.app.state.container.engine.begin() as conn:
            conn.execute(
                update(intents)
                .where(intents.c.task_id == task["id"], intents.c.kind == "reason")
                .values(next_retry_at="2026-01-01T00:00:00Z")
            )
        second = runner.run_once()
        runner.wait_for_idle()
    finally:
        runner.close()

    assert first.claimed == 1
    assert second.claimed == 1
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    reason = next(item for item in graph["intents"] if item["kind"] == "reason")
    assert reason["status"] == "completed"
    assert reason["retry_count"] == 1
    assert reason["next_retry_at"] is None
    assert reason["last_failure_kind"] == ""
    assert reason["failure_class"] == ""
    assert reason["activity_kind"] == "completed"
