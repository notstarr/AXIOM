from __future__ import annotations

from sqlalchemy import update

from axiom.modules.execution.service import reason_prompt_profile_for_trigger
from axiom.modules.reason.tables import reason_runs
from protocol_helpers import complete_bootstrap


BOOTSTRAP_FILES = {
    "asset_inventory.md": "# Assets\n\n- a.example.com",
    "initial_leads.md": "# Leads\n\n- Review authorization boundaries",
    "findings.md": "# Findings\n\n- No confirmed vulnerability in bootstrap",
}


def _claim_reason(client) -> tuple[dict, str, dict]:
    project = client.post("/api/v1/projects", json={"title": "Legacy Plan isolation"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Legacy Plan isolation",
            "origin": "a.example.com",
            "goal": "Cover the attack surface",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore", "plan"], "max_running": 4},
    )
    bootstrap_claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    assert client.post(
        f"{bootstrap_base}/start", json={"lease_id": bootstrap_claim["lease_id"]}
    ).status_code == 200
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap_claim["lease_id"],
        files=BOOTSTRAP_FILES,
    )
    reason_run_id = completed["next_reason_run_id"]
    claim_response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}/claim",
        json={"worker_name": "worker-a"},
    )
    assert claim_response.status_code == 200, claim_response.text
    return task, reason_run_id, claim_response.json()


def test_legacy_plan_reason_run_is_queryable_but_rejects_reason_runtime(
    client, engine
) -> None:
    assert reason_prompt_profile_for_trigger("plan") == "default"
    task, reason_run_id, claim = _claim_reason(client)
    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == reason_run_id)
            .values(trigger="plan")
        )

    query = client.get(f"/api/v1/tasks/{task['id']}/reason-runs/{reason_run_id}")
    assert query.status_code == 200, query.text
    assert query.json()["trigger"] == "plan"

    base = f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}"
    lease = {"lease_id": claim["lease_id"]}
    responses = [
        client.post(f"{base}/start", json=lease),
        client.post(f"{base}/input", json=lease),
        client.post(f"{base}/heartbeat", json=lease),
        client.post(
            f"{base}/activity",
            json={
                **lease,
                "activity_kind": "legacy_plan",
                "activity_summary": "must stay read-only",
                "activity_tool": "test",
            },
        ),
        client.post(f"{base}/conclude", json=lease),
        client.post(
            f"{base}/release",
            json={**lease, "failure_kind": "legacy", "failure_class": "execution"},
        ),
    ]
    assert [response.status_code for response in responses] == [409] * len(responses)
    assert all("read-only" in response.text for response in responses)


def test_legacy_plan_reason_workspace_is_queryable_but_read_only(client, engine) -> None:
    task, reason_run_id, claim = _claim_reason(client)
    base = f"/api/v1/dispatch/tasks/{task['id']}/reason-runs/{reason_run_id}"
    lease = {"lease_id": claim["lease_id"]}
    assert client.post(f"{base}/start", json=lease).status_code == 200
    execution = client.post(f"{base}/input", json=lease)
    assert execution.status_code == 200, execution.text
    workspace_response = client.post(
        f"/api/v1/tasks/{task['id']}/reason/workspaces",
        json={
            "reason_run_id": reason_run_id,
            "lease_id": claim["lease_id"],
            "context_revision": execution.json()["context_revision"],
        },
    )
    assert workspace_response.status_code == 200, workspace_response.text
    workspace = workspace_response.json()

    with engine.begin() as conn:
        conn.execute(
            update(reason_runs)
            .where(reason_runs.c.task_id == task["id"], reason_runs.c.id == reason_run_id)
            .values(trigger="plan")
        )

    query = client.get(f"/api/v1/reason-workspaces/{workspace['id']}")
    assert query.status_code == 200, query.text

    workspace_base = f"/api/v1/reason-workspaces/{workspace['id']}"
    responses = [
        client.post(f"{workspace_base}/status", json=lease),
        client.post(
            f"{workspace_base}/abandon",
            json={**lease, "failure_reason": "must stay read-only"},
        ),
        client.post(f"{workspace_base}/commit", json=lease),
    ]
    assert [response.status_code for response in responses] == [409] * len(responses)
    assert all("read-only" in response.text for response in responses)
