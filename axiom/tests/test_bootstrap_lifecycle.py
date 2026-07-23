from __future__ import annotations

from pathlib import Path


def setup_claimed_bootstrap(client) -> tuple[dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "攻击面发现",
            "origin": "https://a.example.com",
            "goal": "完成渗透测试",
            "hints": ["优先识别后端接口"],
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    return task, claim


def bootstrap_payload(lease_id: str) -> dict:
    return {
        "lease_id": lease_id,
        "summary": "完成初始攻击面收集，确认一个 HTTPS 服务，并记录后续接口测试方向。",
        "artifacts": {
            "asset_inventory": "# 资产清单\n\n- a.example.com:443 HTTPS",
            "initial_leads": "# 初始线索\n\n- 后续验证 API 授权边界",
            "findings": "# 已确认问题\n\n- 本轮没有确认漏洞",
        },
    }


def test_bootstrap_closeout_is_atomic_and_materializes_registered_files(client, data_dir) -> None:
    task, claim = setup_claimed_bootstrap(client)
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"

    started = client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    assert started.status_code == 200, started.text
    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    ).json()
    assert execution_input["origin"] == "https://a.example.com"
    assert execution_input["hints"] == ["优先识别后端接口"]
    assert Path(execution_input["run_dir"]).is_relative_to(data_dir)
    assert client.post(
        f"{base}/heartbeat",
        json={"lease_id": claim["lease_id"]},
    ).status_code == 204

    completed = client.post(
        f"{base}/bootstrap-complete",
        json=bootstrap_payload(claim["lease_id"]),
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["fact_id"] == "f001"
    assert body["next_intent_id"] == "i002"
    assert [item["artifact_type"] for item in body["artifacts"]] == [
        "bootstrap.asset_inventory",
        "bootstrap.initial_leads",
        "bootstrap.findings",
    ]

    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert task_after["phase"] == "reason"
    assert graph["intents"][0]["status"] == "completed"
    assert graph["intents"][0]["to_fact_id"] == "f001"
    assert any(fact["id"] == "f001" for fact in graph["facts"])

    artifacts = client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()
    assert len(artifacts) == 3
    for artifact in artifacts:
        content = client.get(
            f"/api/v1/tasks/{task['id']}/artifacts/{artifact['id']}/content"
        )
        assert content.status_code == 200
        assert content.json()["content"].startswith("# ")
        assert (data_dir / artifact["storage_key"]).is_file()


def test_bootstrap_completion_is_idempotent_for_the_same_lease(client) -> None:
    task, claim = setup_claimed_bootstrap(client)
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    payload = bootstrap_payload(claim["lease_id"])

    first = client.post(f"{base}/bootstrap-complete", json=payload)
    second = client.post(f"{base}/bootstrap-complete", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()) == 3


def test_release_counts_failure_class_and_makes_intent_claimable_again(client) -> None:
    task, claim = setup_claimed_bootstrap(client)
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})

    released = client.post(
        f"{base}/release",
        json={
            "lease_id": claim["lease_id"],
            "failure_kind": "provider_unavailable",
            "failure_class": "transient",
        },
    )

    assert released.status_code == 200, released.text
    intent = client.get(f"/api/v1/tasks/{task['id']}/graph").json()["intents"][0]
    assert intent["status"] == "pending"
    assert intent["worker"] is None
    assert intent["retry_count"] == 0
    assert intent["infrastructure_retry_count"] == 1
    assert intent["last_failure_kind"] == "provider_unavailable"


def test_old_lease_cannot_heartbeat_after_task_stop(client) -> None:
    task, claim = setup_claimed_bootstrap(client)
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    client.post(f"/api/v1/tasks/{task['id']}/stop")

    stale = client.post(f"{base}/heartbeat", json={"lease_id": claim["lease_id"]})

    assert stale.status_code == 409
    assert stale.json()["error"]["message"] == "Intent lease is no longer active"
