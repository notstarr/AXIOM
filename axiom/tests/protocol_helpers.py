from __future__ import annotations

from pathlib import Path


DEFAULT_BOOTSTRAP = {
    "asset_inventory.md": "# 资产\n\n- a.example.com",
    "initial_leads.md": "# 线索\n\n- 验证对象级授权",
    "findings.md": "# 已确认问题\n\n- 本轮无确认漏洞",
}


def write_bootstrap_files(run_dir: str | Path, files: dict[str, str] | None = None) -> None:
    root = Path(run_dir)
    for filename, content in (files or DEFAULT_BOOTSTRAP).items():
        (root / filename).write_text(content, encoding="utf-8")


def bootstrap_files_from_legacy(payload: dict) -> dict[str, str]:
    artifacts = payload.get("artifacts", payload)
    return {
        "asset_inventory.md": artifacts["asset_inventory"],
        "initial_leads.md": artifacts["initial_leads"],
        "findings.md": artifacts["findings"],
    }


def complete_bootstrap(
    client,
    *,
    task_id: str,
    intent_id: str,
    lease_id: str,
    files: dict[str, str] | None = None,
) -> dict:
    base = f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}"
    execution_input = client.post(f"{base}/input", json={"lease_id": lease_id}).json()
    write_bootstrap_files(execution_input["run_dir"], files)
    response = client.post(f"{base}/bootstrap-complete", json={"lease_id": lease_id})
    assert response.status_code == 200, response.text
    return response.json()


def claim_reason_run(
    client,
    *,
    task_id: str,
    reason_run_id: str,
    worker_name: str = "worker-a",
) -> tuple[dict, dict]:
    base = f"/api/v1/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}"
    claim_response = client.post(
        f"{base}/claim",
        json={"worker_name": worker_name},
    )
    assert claim_response.status_code == 200, claim_response.text
    claim = claim_response.json()
    lease = {"lease_id": claim["lease_id"]}
    started = client.post(f"{base}/start", json=lease)
    assert started.status_code == 200, started.text
    input_response = client.post(f"{base}/input", json=lease)
    assert input_response.status_code == 200, input_response.text
    execution_input = input_response.json()
    claim["context_revision"] = execution_input["context_revision"]
    return claim, execution_input


def open_reason_workspace(client, *, task_id: str, claim: dict) -> dict:
    response = client.post(
        f"/api/v1/tasks/{task_id}/reason/workspaces",
        json={
            "reason_run_id": claim["reason_run_id"],
            "lease_id": claim["lease_id"],
            "context_revision": claim["context_revision"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def update_runtime_settings(client, **changes) -> dict:
    current = client.get("/api/v1/settings/runtime").json()
    config = {**current["config"], **changes}
    response = client.put(
        "/api/v1/settings/runtime",
        json={
            "actor": "test",
            "expected_version": current["version"],
            "config": config,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def apply_reason_draft(client, workspace_id: str, lease_id: str, draft: dict):
    base = f"/api/v1/reason-workspaces/{workspace_id}"
    lease = {"lease_id": lease_id}
    for todo in draft.get("todo_adds", []):
        response = client.post(f"{base}/todos", json={**lease, "todo": todo})
        if response.is_error:
            return response
    for update in draft.get("todo_updates", []):
        response = client.post(f"{base}/todo-updates", json={**lease, "update": update})
        if response.is_error:
            return response
    for chain in draft.get("chain_adds", []):
        response = client.post(f"{base}/chains", json={**lease, "chain": chain})
        if response.is_error:
            return response
    for intent in draft.get("intent_proposals", []):
        response = client.post(f"{base}/intents", json={**lease, "intent": intent})
        if response.is_error:
            return response
    for update in draft.get("chain_updates", []):
        response = client.post(f"{base}/chain-updates", json={**lease, "update": update})
        if response.is_error:
            return response
    assessment = draft.get("goal_assessment")
    if assessment is None:
        assessment = {
            "achieved": False,
            "reason": "当前 Goal 尚未达成，继续执行已规划的 Explore Intent",
            "evidence_refs": [],
        }
    response = client.post(
        f"{base}/goal-assessment",
        json={**lease, "assessment": assessment},
    )
    if response.is_error:
        return response
    return client.post(f"{base}/commit", json=lease)
