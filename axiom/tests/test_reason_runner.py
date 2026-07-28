from __future__ import annotations

from sqlalchemy import update

from axiom.dispatcher.runner import AxiomRunner
from axiom.dispatcher.schemas import (
    ClaimAction,
    ClaimedIntent,
    ClaimedWork,
    DispatcherSnapshot,
)
from axiom.modules.execution.schemas import ExecutionInput
from axiom.modules.reason.schemas import (
    ClaimedReasonRun,
    CommitResult,
    ReasonWorkspace,
    WorkspaceStatus,
)
from axiom.modules.reason.tables import reason_runs
from axiom.modules.workers.schemas import Worker
from protocol_helpers import bootstrap_files_from_legacy, complete_bootstrap


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

    def claim_work(self, action: ClaimAction) -> ClaimedWork | None:
        if action.reason_run_id:
            path = (
                f"/api/v1/dispatch/tasks/{action.task_id}/reason-runs/"
                f"{action.reason_run_id}/claim"
            )
            model = ClaimedReasonRun
        else:
            assert action.intent_id
            path = (
                f"/api/v1/dispatch/tasks/{action.task_id}/intents/"
                f"{action.intent_id}/claim"
            )
            model = ClaimedIntent
        response = self.client.post(path, json={"worker_name": action.worker_name})
        if response.status_code == 409:
            return None
        assert response.status_code == 200, response.text
        return model.model_validate(response.json())

    def start(self, work: ClaimedWork) -> None:
        response = self.client.post(self._url(work, "start"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text

    def heartbeat(self, work: ClaimedWork) -> None:
        response = self.client.post(
            self._url(work, "heartbeat"),
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 204, response.text

    def input(self, work: ClaimedWork) -> ExecutionInput:
        response = self.client.post(self._url(work, "input"), json={"lease_id": work.lease_id})
        assert response.status_code == 200, response.text
        return ExecutionInput.model_validate(response.json())

    def open_reason_workspace(self, work: ClaimedReasonRun, *, context_revision: int):
        response = self.client.post(
            f"/api/v1/tasks/{work.task_id}/reason/workspaces",
            json={
                "reason_run_id": work.reason_run_id,
                "lease_id": work.lease_id,
                "context_revision": context_revision,
            },
        )
        assert response.status_code == 200, response.text
        return ReasonWorkspace.model_validate(response.json())

    def reason_workspace_status(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
    ) -> WorkspaceStatus:
        response = self.client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/status",
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 200, response.text
        return WorkspaceStatus.model_validate(response.json())

    def commit_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
    ) -> CommitResult:
        response = self.client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/commit",
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 200, response.text
        return CommitResult.model_validate(response.json())

    def begin_conclude(self, work: ClaimedReasonRun) -> None:
        response = self.client.post(
            self._url(work, "conclude"),
            json={"lease_id": work.lease_id},
        )
        assert response.status_code == 204, response.text

    def abandon_reason_workspace(
        self,
        work: ClaimedReasonRun,
        *,
        workspace_id: str,
        failure_reason: str,
    ) -> None:
        response = self.client.post(
            f"/api/v1/reason-workspaces/{workspace_id}/abandon",
            json={"lease_id": work.lease_id, "failure_reason": failure_reason},
        )
        assert response.status_code == 200, response.text

    def release(self, work: ClaimedWork, command) -> None:
        response = self.client.post(
            self._url(work, "release"),
            json=command.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def _url(work: ClaimedWork, action: str) -> str:
        if isinstance(work, ClaimedReasonRun):
            return (
                f"/api/v1/dispatch/tasks/{work.task_id}/reason-runs/"
                f"{work.reason_run_id}/{action}"
            )
        return f"/api/v1/dispatch/tasks/{work.task_id}/intents/{work.intent_id}/{action}"


class FakeReasonExecutor:
    def __init__(self, client, draft: dict | None):
        self.client = client
        self.draft = draft

    def execute(self, _execution_input, *, workspace, lease_active):
        assert lease_active()
        if self.draft is not None:
            self._stage(workspace)
        return "FINISH"

    def conclude(self, _execution_input, *, workspace, reason, lease_active):
        assert lease_active()
        assert reason
        return "FINISH"

    def _stage(self, workspace: ReasonWorkspace) -> None:
        base = f"/api/v1/reason-workspaces/{workspace.id}"
        lease = {"lease_id": workspace.lease_id}
        for todo in self.draft.get("todo_adds", []):
            response = self.client.post(f"{base}/todos", json={**lease, "todo": todo})
            assert response.status_code == 200, response.text
        for update_command in self.draft.get("todo_updates", []):
            response = self.client.post(
                f"{base}/todo-updates",
                json={**lease, "update": update_command},
            )
            assert response.status_code == 200, response.text
        for proposal in self.draft.get("intent_proposals", []):
            response = self.client.post(
                f"{base}/intents",
                json={**lease, "intent": proposal},
            )
            assert response.status_code == 200, response.text
        assessment = self.draft.get(
            "goal_assessment",
            {
                "achieved": False,
                "reason": "当前仍需执行本轮规划的 Explore Intent。",
                "evidence_refs": ["fact:f001"],
            },
        )
        response = self.client.post(
            f"{base}/goal-assessment",
            json={**lease, "assessment": assessment},
        )
        assert response.status_code == 200, response.text


def _pending_reason(client) -> tuple[dict, str, ApiControlPlane]:
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
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap["lease_id"],
        files=bootstrap_files_from_legacy(BOOTSTRAP),
    )
    return task, completed["next_reason_run_id"], ApiControlPlane(client)


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
                "conclude_seconds": 180,
            }
        ],
    }


def test_reason_runner_commits_a_plan_without_reason_choosing_a_worker(client) -> None:
    task, reason_run_id, control = _pending_reason(client)
    drafts = [_valid_plan()]
    runner = AxiomRunner(
        control,
        executor_factory=lambda _worker, _work: FakeReasonExecutor(client, drafts.pop(0)),
        heartbeat_interval_seconds=60,
    )
    try:
        report = runner.run_once()
        runner.wait_for_idle()
    finally:
        runner.close()

    assert report.claimed == 1
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert all(item["kind"] != "reason" for item in graph["intents"])
    explore = next(item for item in graph["intents"] if item["kind"] == "explore")
    assert explore["id"] == "i002"
    assert explore["status"] == "pending"
    reason_run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}"
    ).json()
    assert reason_run["status"] == "completed"
    assert reason_run["activity_kind"] == "completed"


def test_invalid_reason_plan_abandons_workspace_and_can_retry_with_a_new_lease(client) -> None:
    task, reason_run_id, control = _pending_reason(client)
    drafts = [None, _valid_plan()]
    runner = AxiomRunner(
        control,
        executor_factory=lambda _worker, _work: FakeReasonExecutor(client, drafts.pop(0)),
        heartbeat_interval_seconds=60,
    )
    try:
        first = runner.run_once()
        runner.wait_for_idle()
        with client.app.state.container.engine.begin() as conn:
            conn.execute(
                update(reason_runs)
                .where(
                    reason_runs.c.task_id == task["id"],
                    reason_runs.c.id == reason_run_id,
                )
                .values(next_retry_at="2026-01-01T00:00:00Z")
            )
        second = runner.run_once()
        runner.wait_for_idle()
    finally:
        runner.close()

    assert first.claimed == 1
    assert second.claimed == 1
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert all(item["kind"] != "reason" for item in graph["intents"])
    reason_run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}"
    ).json()
    assert reason_run["status"] == "completed"
    assert reason_run["retry_count"] == 1
    assert reason_run["next_retry_at"] is None
    assert reason_run["last_failure_kind"] == ""
    assert reason_run["failure_class"] == ""
    assert reason_run["activity_kind"] == "completed"
