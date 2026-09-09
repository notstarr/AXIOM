from __future__ import annotations

from pathlib import Path

import pytest

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
    # Evidence files without a referencing vulnerability stay copied to
    # storage but no longer get their own index rows.
    assert [item["artifact_type"] for item in completed["artifacts"]] == [
        "explore.findings",
        "explore.leads",
    ]
    assert [item["storage_key"].split("/")[-2:] for item in completed["artifacts"][:2]] == [
        [intents[0], "findings.md"],
        [intents[0], "leads.md"],
    ]
    evidence_dir = data_dir / "tasks" / task["id"] / "artifacts" / intents[0] / "evidence"
    assert evidence_dir.is_dir() and any(evidence_dir.iterdir())
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
    assert task_after["active_reason_run"] is None
    assert [item["id"] for item in task_after["queued_reason_runs"]] == [
        completed["reason_run_id"]
    ]


def test_explore_human_intervention_marker_is_durable_idempotent_and_visible(
    client,
) -> None:
    task, intent_ids = _task_with_explore_intents(client)
    intent_id = intent_ids[0]
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}"
    claim = client.post(
        f"{base}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    task_phase = client.get(f"/api/v1/tasks/{task['id']}").json()["phase"]

    first = client.post(
        f"{base}/human-intervention",
        json={
            "lease_id": claim["lease_id"],
            "reason": "需要人工提供短信验证码，当前 Lease 无法接收该验证码",
        },
    )
    assert first.status_code == 200, first.text
    first_marker = first.json()
    assert first_marker["required"] is True
    assert first_marker["updated"] is True

    repeated = client.post(
        f"{base}/human-intervention",
        json={
            "lease_id": claim["lease_id"],
            "reason": "需要人工提供短信验证码，当前 Lease 无法接收该验证码",
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["updated"] is False
    assert repeated.json()["marked_at"] == first_marker["marked_at"]

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    claimed_intent = next(item for item in graph["intents"] if item["id"] == intent_id)
    assert claimed_intent["status"] == "claimed"
    assert claimed_intent["retry_count"] == 0
    assert claimed_intent["infrastructure_retry_count"] == 0
    assert claimed_intent["human_intervention_required"] is True
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["phase"] == task_phase

    started = client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    assert started.status_code == 200, started.text
    changed = client.post(
        f"{base}/human-intervention",
        json={
            "lease_id": claim["lease_id"],
            "reason": "需要人工在真实设备完成扫码授权，当前工具无法代替设备确认",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["updated"] is True
    assert changed.json()["marked_at"] != first_marker["marked_at"]

    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    ).json()
    artifacts_dir = Path(execution_input["run_dir"]) / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "findings.md").write_text(
        "# 已确认事实\n\n本轮未确认漏洞。\n",
        encoding="utf-8",
    )
    (artifacts_dir / "leads.md").write_text(
        "# 待验证线索\n\n需要人工在真实设备完成扫码授权。\n",
        encoding="utf-8",
    )
    concluding = client.post(
        f"{base}/conclude",
        json={"lease_id": claim["lease_id"]},
    )
    assert concluding.status_code == 204, concluding.text
    concluded_marker = client.post(
        f"{base}/human-intervention",
        json={
            "lease_id": claim["lease_id"],
            "reason": "需要人工在真实设备完成扫码授权，当前 Lease 无法执行设备手工确认",
        },
    )
    assert concluded_marker.status_code == 200, concluded_marker.text
    assert concluded_marker.json()["updated"] is True

    completed = client.post(
        f"{base}/explore-complete",
        json={"lease_id": claim["lease_id"]},
    )
    assert completed.status_code == 200, completed.text

    graph = client.get(f"/api/v1/tasks/{task['id']}/graph").json()
    producing_intent = next(item for item in graph["intents"] if item["id"] == intent_id)
    assert producing_intent["status"] == "completed"
    assert producing_intent["to_fact_id"] == completed.json()["fact_id"]
    assert producing_intent["human_intervention_required"] is True
    assert producing_intent["human_intervention_reason"] == (
        "需要人工在真实设备完成扫码授权，当前 Lease 无法执行设备手工确认"
    )
    assert producing_intent["human_intervention_marked_at"] == (
        concluded_marker.json()["marked_at"]
    )
    assert any(item["id"] == completed.json()["fact_id"] for item in graph["facts"])

    events = client.get(
        "/api/v1/audit-events",
        params={
            "task_id": task["id"],
            "intent_id": intent_id,
            "event_type": "intent.human_intervention_marked",
        },
    ).json()
    assert len(events) == 3
    assert all(item["level"] == "warning" for item in events)
    assert events[-1]["payload"] == {
        "phase": "explore",
        "reason": "需要人工提供短信验证码，当前 Lease 无法接收该验证码",
    }
    assert all("lease_id" not in item["payload"] for item in events)


@pytest.mark.parametrize("reason", ["", "   ", "x" * 2_001])
def test_explore_human_intervention_marker_validates_reason(client, reason: str) -> None:
    task, intent_ids = _task_with_explore_intents(client)
    intent_id = intent_ids[0]
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}/claim",
        json={"worker_name": "worker-a"},
    ).json()

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}/human-intervention",
        json={"lease_id": claim["lease_id"], "reason": reason},
    )

    assert response.status_code == 422, response.text


def test_explore_human_intervention_marker_rejects_wrong_or_inactive_lease(client) -> None:
    task, intent_ids = _task_with_explore_intents(client)
    intent_id = intent_ids[0]
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intent_id}"
    claim = client.post(f"{base}/claim", json={"worker_name": "worker-a"}).json()
    reason = "需要人工完成真实支付，当前 Lease 无法安全替代该动作"

    wrong_lease = client.post(
        f"{base}/human-intervention",
        json={"lease_id": "wrong-lease", "reason": reason},
    )
    assert wrong_lease.status_code == 409, wrong_lease.text

    released = client.post(
        f"{base}/release",
        json={"lease_id": claim["lease_id"], "failure_kind": "test_release"},
    )
    assert released.status_code == 200, released.text
    inactive = client.post(
        f"{base}/human-intervention",
        json={"lease_id": claim["lease_id"], "reason": reason},
    )
    assert inactive.status_code == 409, inactive.text


def test_human_intervention_marker_rejects_non_explore_intent(client) -> None:
    update_runtime_settings(client, reason_min_interval_seconds=0)
    project = client.post("/api/v1/projects", json={"title": "系统"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Bootstrap 标记拒绝",
            "origin": "a.example.com",
            "goal": "验证阶段限制",
            "worker_pool": ["worker-a"],
        },
    ).json()
    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap", "reason", "explore"], "max_running": 8},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()

    response = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/human-intervention",
        json={
            "lease_id": claim["lease_id"],
            "reason": "需要人工完成真实设备操作",
        },
    )

    assert response.status_code == 409, response.text
    assert "not an Explore Intent" in response.text


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
        "prerequisites": "需要任意低权限账号。",
        "impact": "导致跨用户数据泄露。",
        "remediation": "服务端校验订单所属用户。",
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
    review_runs = client.get(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/v001/reviews"
    )
    assert review_runs.status_code == 200, review_runs.text
    assert [(item["status"], item["trigger"]) for item in review_runs.json()] == [
        ("pending", "reported")
    ]
    assert review_runs.json()[0]["trigger_payload"] == {
        "source": "explore",
        "intent_id": intents[0],
    }
    fact = next(
        item
        for item in client.get(f"/api/v1/tasks/{task['id']}/graph").json()["facts"]
        if item["id"] == completed["fact_id"]
    )
    assert f"intent:{completed['intent_id']} 完成" in fact["description"]
    assert "已完成 Explore Intent" not in fact["description"]
    assert "vulns:v001" in fact["description"]
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
                "prerequisites": "无需额外权限",
                "impact": "影响",
                "remediation": "限制敏感信息返回",
                "evidence_paths": ["evidence/proof.txt"],
                "status": "忽略",
                "status_reason": "不够",
            },
        },
    )
    assert response.status_code == 422
    assert "status" in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prerequisites", None),
        ("prerequisites", "   "),
        ("remediation", None),
        ("remediation", "   "),
    ],
)
def test_explore_api_requires_prerequisites_and_remediation(
    client,
    field: str,
    value: str | None,
) -> None:
    task, intents = _task_with_explore_intents(client)
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/{intents[0]}"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    report = {
        "title": "对象级授权缺失",
        "type": "越权",
        "severity": "high",
        "description": "低权限用户可以读取其他用户对象。",
        "prerequisites": "需要任意低权限账号。",
        "impact": "导致跨用户数据泄露。",
        "remediation": "服务端校验对象归属。",
    }
    if value is None:
        report.pop(field)
    else:
        report[field] = value

    response = client.post(
        f"{base}/explore-vulnerabilities",
        json={"lease_id": claim["lease_id"], "report": report},
    )

    assert response.status_code == 422
    assert field in response.text


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
                "prerequisites": "需要低权限账号",
                "impact": "影响",
                "remediation": "校验对象归属",
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
                "prerequisites": "需要任意低权限账号。",
                "impact": "可能造成跨用户数据泄露。",
                "remediation": "服务端校验对象归属。",
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
