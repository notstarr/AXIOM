from __future__ import annotations

from pathlib import Path

from protocol_helpers import (
    bootstrap_files_from_legacy,
    claim_reason_run,
    complete_bootstrap,
    open_reason_workspace,
    update_runtime_settings,
)

BOOTSTRAP = {
    "summary": "完成初始攻击面发现。",
    "artifacts": {
        "asset_inventory": "# 资产\n\n- a.example.com",
        "initial_leads": "# 线索\n\n- 验证对象级授权",
        "findings": "# 已确认问题\n\n- 本轮无确认漏洞",
    },
}


def _task_with_explore_intents(client, *, count: int = 1) -> tuple[dict, list[str]]:
    update_runtime_settings(client, reason_min_interval_seconds=0)
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Explore 生命周期",
            "origin": "a.example.com",
            "goal": "验证 Explore 原子收口",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 8},
    )
    bootstrap = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    bootstrap_base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{bootstrap_base}/start", json={"lease_id": bootstrap["lease_id"]})
    completed = complete_bootstrap(
        client,
        task_id=task["id"],
        intent_id="i001",
        lease_id=bootstrap["lease_id"],
        files=bootstrap_files_from_legacy(BOOTSTRAP),
    )
    reason, _execution_input = claim_reason_run(
        client,
        task_id=task["id"],
        reason_run_id=completed["next_reason_run_id"],
        worker_name="worker-a",
    )
    workspace = open_reason_workspace(client, task_id=task["id"], claim=reason)
    draft = {
        "todo_adds": [
            {
                "ref": f"todo-{number}",
                "title": f"验证测试方向 {number}",
                "category": "authorization",
                "sources": [{"type": "fact", "id": completed["fact_id"]}],
            }
            for number in range(count)
        ],
        "todo_updates": [],
        "intent_proposals": [
            {
                "todo_ref": f"todo-{number}",
                "from_facts": [completed["fact_id"]],
                "description": f"验证接口方向 {number} 的授权边界",
                "targets": ["a.example.com"],
                "execute_seconds": 300,
                "conclude_seconds": 180,
            }
            for number in range(count)
        ],
    }
    for todo in draft["todo_adds"]:
        response = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/todos",
            json={"lease_id": reason["lease_id"], "todo": todo},
        )
        assert response.status_code == 200, response.text
    result = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/goal-assessment",
        json={
            "lease_id": reason["lease_id"],
            "assessment": {
                "achieved": False,
                "reason": "当前仍需 Explore 独立验证规划中的测试方向。",
                "evidence_refs": [f"fact:{completed['fact_id']}"],
            },
        },
    )
    assert result.status_code == 200, result.text
    for intent in draft["intent_proposals"]:
        response = client.post(
            f"/api/v1/reason-workspaces/{workspace['id']}/intents",
            json={"lease_id": reason["lease_id"], "intent": intent},
        )
        assert response.status_code == 200, response.text
    committed = client.post(
        f"/api/v1/reason-workspaces/{workspace['id']}/commit",
        json={"lease_id": reason["lease_id"]},
    )
    assert committed.status_code == 200, committed.text
    return task, committed.json()["created_intent_ids"]


def _complete_explore(
    client,
    task_id: str,
    intent_id: str,
    *,
    number: int = 1,
    vulnerability: dict | None = None,
) -> dict:
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task_id}/intents/{intent_id}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    ).json()
    artifacts_dir = Path(execution_input["run_dir"]) / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "findings.md").write_text(
        f"# 已确认事实\n\n- 已完成测试方向 {number}\n",
        encoding="utf-8",
    )
    (artifacts_dir / "leads.md").write_text(
        "# 待验证线索\n\n本轮无遗留待验证线索\n",
        encoding="utf-8",
    )
    evidence_dir = artifacts_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / f"response-{number}.md").write_text(
        f"# 证据 {number}\n\nGET /api/orders/{number}",
        encoding="utf-8",
    )
    if vulnerability is not None:
        report = client.post(
            f"{base}/explore-vulnerabilities",
            json={"lease_id": claim["lease_id"], "report": vulnerability},
        )
        assert report.status_code == 200, report.text
        duplicate = client.post(
            f"{base}/explore-vulnerabilities",
            json={"lease_id": claim["lease_id"], "report": vulnerability},
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["duplicate"] is True
    response = client.post(
        f"{base}/explore-complete",
        json={"lease_id": claim["lease_id"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_explore_completion_atomically_registers_artifact_fact_and_reason(client, data_dir) -> None:
    task, intents = _task_with_explore_intents(client)

    completed = _complete_explore(client, task["id"], intents[0])

    assert completed["fact_id"] == "f002"
    assert completed["reason_run_id"] == "rr000002"
    assert [item["artifact_type"] for item in completed["artifacts"]] == [
        "explore.findings",
        "explore.leads",
        "explore.evidence",
    ]
    assert [item["storage_key"].split("/")[-2:] for item in completed["artifacts"][:2]] == [
        [intents[0], "findings.md"],
        [intents[0], "leads.md"],
    ]
    evidence = completed["artifacts"][2]
    assert (data_dir / evidence["storage_key"]).read_text(encoding="utf-8").startswith("# 证据")
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    explore = next(item for item in graph["intents"] if item["id"] == intents[0])
    assert [item["id"] for item in graph["intents"]] == ["i001", "i002"]
    assert explore["status"] == "completed"
    assert explore["to_fact_id"] == "f002"
    run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{completed['reason_run_id']}"
    ).json()
    assert run["source_fact_ids"] == ["f002"]
    task_after = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert task_after["phase"] == "reason"
    assert task_after["active_reason_run"]["id"] == completed["reason_run_id"]


def test_reason_trigger_batches_new_facts_while_explore_backlog_remains(client) -> None:
    task, intent_ids = _task_with_explore_intents(client, count=4)

    first = _complete_explore(client, task["id"], intent_ids[0], number=1)
    assert first["reason_run_id"] is None
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == "explore"

    second = _complete_explore(client, task["id"], intent_ids[1], number=2)
    assert second["reason_run_id"] == "rr000002"
    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    assert all(item["kind"] != "reason" for item in graph["intents"])
    reason_run = client.get(
        f"/api/v1/tasks/{task['id']}/reason-runs/{second['reason_run_id']}"
    ).json()
    assert reason_run["source_fact_ids"] == ["f002", "f003"]


def test_explore_completion_is_idempotent_for_the_same_lease(client) -> None:
    task, intents = _task_with_explore_intents(client)
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    ).json()
    artifacts_dir = Path(execution_input["run_dir"]) / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "findings.md").write_text("# 已确认事实\n\n完整内容", encoding="utf-8")
    (artifacts_dir / "leads.md").write_text("# 待验证线索\n\n无", encoding="utf-8")
    payload = {"lease_id": claim["lease_id"]}

    first = client.post(f"{base}/explore-complete", json=payload)
    second = client.post(f"{base}/explore-complete", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(
        [
            item
            for item in client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()
            if item["intent_id"] == intents[0]
        ]
    ) == 2


def test_explore_reports_vulnerability_incrementally_and_fact_keeps_its_reference(
    client,
    data_dir,
) -> None:
    task, intents = _task_with_explore_intents(client)
    vulnerability = {
        "title": "订单对象级授权缺失",
        "type": "越权",
        "severity": "high",
        "description": "低权限用户可以读取其他订单详情。",
        "impact": "导致跨用户数据泄露。",
        "evidence_paths": ["evidence/response-1.md"],
        "affected_target": "https://a.example.com/api/orders/2",
    }

    completed = _complete_explore(
        client,
        task["id"],
        intents[0],
        vulnerability=vulnerability,
    )

    assert completed["vulnerabilities"][0]["id"] == "v001"
    assert completed["vulnerabilities"][0]["source"] == "explore"
    assert completed["vulnerabilities"][0]["reported_by"] == "worker-a"
    assert completed["vulnerabilities"][0]["status"] == "待验证"
    assert completed["vulnerabilities"][0]["finding_artifact"]["artifact_type"] == "explore.findings"
    assert [item["artifact_type"] for item in completed["vulnerabilities"][0]["evidence_artifacts"]] == ["explore.evidence"]
    fact = next(
        item
        for item in client.get(f"/api/v1/tasks/{task['id']}/graph").json()["facts"]
        if item["id"] == completed["fact_id"]
    )
    assert "已完成 Explore Intent" in fact["description"]
    assert "。。" not in fact["description"]
    assert "vulnerability:v001" in fact["description"]
    # The run directory is lease-specific; the durable artifact must be the
    # only retained copy of the evidence file.
    assert not any(
        path.name == "response-1.md"
        for path in (data_dir / "runs" / task["id"] / intents[0]).rglob("*")
    )
    artifacts = client.get(f"/api/v1/tasks/{task['id']}/artifacts").json()
    assert len([item for item in artifacts if item["intent_id"] == intents[0]]) == 3


def test_explore_rejects_worker_supplied_status(client) -> None:
    task, intents = _task_with_explore_intents(client)
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    ).json()
    artifacts = Path(execution_input["run_dir"]) / "artifacts"
    (artifacts / "evidence").mkdir(parents=True)
    (artifacts / "findings.md").write_text("# 已确认事实\n\n漏洞", encoding="utf-8")
    (artifacts / "leads.md").write_text("# 待验证线索\n\n无", encoding="utf-8")
    (artifacts / "evidence" / "proof.txt").write_text("proof", encoding="utf-8")
    response = client.post(
        f"{base}/explore-vulnerabilities",
        json={
            "lease_id": claim["lease_id"],
            "report": {
                "title": "待忽略问题",
                "type": "信息泄露",
                "severity": "low",
                "description": "描述",
                "impact": "影响",
                "evidence_paths": ["evidence/proof.txt"],
                "status": "忽略",
                "status_reason": "不够",
            },
        },
    )
    assert response.status_code == 422
    assert "status" in response.text


def test_explore_keeps_reported_vulnerability_when_evidence_file_is_missing(client) -> None:
    task, intents = _task_with_explore_intents(client)
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    execution_input = client.post(f"{base}/input", json={"lease_id": claim["lease_id"]}).json()
    artifacts = Path(execution_input["run_dir"]) / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "findings.md").write_text("已确认事实", encoding="utf-8")
    (artifacts / "leads.md").write_text("待验证线索", encoding="utf-8")
    report = client.post(
        f"{base}/explore-vulnerabilities",
        json={
            "lease_id": claim["lease_id"],
            "report": {
                "title": "缺失证据",
                "type": "越权",
                "severity": "high",
                "description": "描述",
                "impact": "影响",
                "evidence_paths": ["evidence/missing.txt"],
            },
        },
    )
    assert report.status_code == 200, report.text

    completed = client.post(
        f"{base}/explore-complete",
        json={"lease_id": claim["lease_id"]},
    )

    assert completed.status_code == 200, completed.text
    vulnerability = completed.json()["vulnerabilities"][0]
    assert vulnerability["finding_artifact"]["artifact_type"] == "explore.findings"
    assert vulnerability["evidence_artifacts"] == []


def test_explore_reports_vulnerability_without_evidence_paths(client) -> None:
    task, intents = _task_with_explore_intents(client)
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    ).json()
    artifacts = Path(execution_input["run_dir"]) / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "findings.md").write_text("已确认越权行为", encoding="utf-8")
    (artifacts / "leads.md").write_text("本轮无遗留待验证线索", encoding="utf-8")
    report = client.post(
        f"{base}/explore-vulnerabilities",
        json={
            "lease_id": claim["lease_id"],
            "report": {
                "title": "无证据文件的待验证越权",
                "type": "越权",
                "severity": "high",
                "description": "当前会话观察到跨用户数据读取。",
                "impact": "可能造成跨用户数据泄露。",
            },
        },
    )
    assert report.status_code == 200, report.text

    completed = client.post(
        f"{base}/explore-complete",
        json={"lease_id": claim["lease_id"]},
    )

    assert completed.status_code == 200, completed.text
    vulnerability = completed.json()["vulnerabilities"][0]
    assert vulnerability["status"] == "待验证"
    assert vulnerability["finding_artifact"]["artifact_type"] == "explore.findings"
    assert vulnerability["evidence_artifacts"] == []
