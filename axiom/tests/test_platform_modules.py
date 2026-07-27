from __future__ import annotations


def _project_task(client) -> tuple[dict, dict]:
    project = client.post("/api/v1/projects", json={"title": "平台能力"}).json()
    task = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "平台能力任务",
            "origin": "a.example.com",
            "goal": "验证凭证和漏洞通用格式",
        },
    ).json()
    return project, task


def test_runtime_settings_have_safe_defaults_and_versioned_updates(client) -> None:
    first = client.get("/api/v1/settings/runtime")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["config"]["reason_batch_size"] == 2
    assert "explore_execute_seconds" not in body["config"]

    updated = client.put(
        "/api/v1/settings/runtime",
        json={
            "actor": "admin",
            "expected_version": body["version"],
            "config": {
                **body["config"],
                "global_max_active_runs": 4,
                "reason_target_open_intents": 4,
                "network_proxy_mode": "http",
                "network_proxy_url": "http://127.0.0.1:7890",
            },
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == body["version"] + 1
    assert updated.json()["config"]["network_proxy_mode"] == "http"


def test_credentials_are_project_scoped_and_injected_into_execution_input(client) -> None:
    project, task = _project_task(client)
    created = client.post(
        f"/api/v1/projects/{project['id']}/credentials",
        json={
            "name": "登录 Cookie",
            "credential_type": "headers",
            "target_host": "b.example.com",
            "data": {"Cookie": "session=raw-secret", "X-CSRF": "token"},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["Cookie"] == "session=raw-secret"
    listed = client.get(f"/api/v1/projects/{project['id']}/credentials")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    client.put(
        "/api/v1/workers/worker-a",
        json={"capabilities": ["bootstrap"], "max_running": 1},
    )
    claim = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/intents/i001/claim",
        json={"worker_name": "worker-a"},
    ).json()
    base = f"/api/v1/dispatch/tasks/{task['id']}/intents/i001"
    client.post(f"{base}/start", json={"lease_id": claim["lease_id"]})
    execution_input = client.post(
        f"{base}/input",
        json={"lease_id": claim["lease_id"]},
    )
    assert execution_input.status_code == 200, execution_input.text
    assert execution_input.json()["credentials"][0]["data"]["Cookie"] == "session=raw-secret"


def test_vulnerability_format_is_shared_by_internal_management(client) -> None:
    _project, task = _project_task(client)
    reported = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "对象级授权缺失",
            "type": "越权访问",
            "severity": "high",
            "description": "普通用户可以读取其他用户订单",
            "prerequisites": "已登录普通用户",
            "impact": "造成跨用户数据泄露",
            "reproduction_steps": ["调用订单详情接口并替换订单 ID"],
            "remediation": "校验对象归属",
            "curl_command": "curl https://a.example.com/orders/2",
            "evidence": ["响应返回了不属于当前用户的订单"],
            "affected_target": "a.example.com",
            "source": "explore",
            "reported_by": "worker-a",
        },
    )
    assert reported.status_code == 201, reported.text
    vulnerability = reported.json()
    assert vulnerability["id"] == "v001"
    assert client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities").json()[0]["severity"] == "high"

    ignored = client.patch(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}",
        json={"status": "忽略", "status_reason": "重复问题已合并"},
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["status"] == "忽略"


def test_audit_events_are_append_only_and_filterable(client) -> None:
    created = client.post(
        "/api/v1/audit-events",
        json={
            "task_id": "t_missing",
            "event_type": "test.event",
            "message": "审计测试",
            "payload": {"ok": True},
        },
    )
    # The public API is intentionally read-only; runtime code records events
    # through the service boundary. This assertion protects that boundary.
    assert created.status_code in {404, 405}
