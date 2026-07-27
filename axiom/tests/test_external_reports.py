from __future__ import annotations


def _task(client, *, enabled: bool = True) -> dict:
    project = client.post("/api/v1/projects", json={"title": "外部上报"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "上报任务",
            "origin": "a.example.com",
            "goal": "验证外部漏洞格式",
            "external_report_enabled": enabled,
        },
    ).json()


def _vulnerability(client, task_id: str) -> dict:
    return client.post(
        f"/api/v1/tasks/{task_id}/vulnerabilities",
        json={
            "title": "越权读取",
            "type": "越权访问",
            "severity": "high",
            "description": "普通用户读取了其他用户资源",
            "impact": "造成数据泄露",
            "source": "explore",
            "reported_by": "worker-a",
            "affected_target": "a.example.com",
        },
    ).json()


def _configure(client, *, max_attempts: int = 3) -> None:
    current = client.get("/api/v1/settings/runtime").json()
    config = current["config"]
    config.update(
        {
            "external_report_base_url": "https://report.example.test/vulnerabilities",
            "external_report_token": "external-secret",
            "external_report_max_attempts": max_attempts,
        }
    )
    response = client.put(
        "/api/v1/settings/runtime",
        json={"actor": "test", "expected_version": current["version"], "config": config},
    )
    assert response.status_code == 200, response.text


def test_preview_uses_the_shared_vulnerability_report_shape_without_exposing_token(client) -> None:
    task = _task(client)
    vulnerability = _vulnerability(client, task["id"])
    _configure(client)

    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-preview"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "POST"
    assert body["endpoint"] == "https://report.example.test/vulnerabilities"
    assert body["payload"]["title"] == "越权读取"
    assert "id" not in body["payload"]
    assert "task_id" not in body["payload"]
    assert "external-secret" not in response.text


def test_external_adapter_includes_text_artifact_content_and_binary_metadata(client) -> None:
    task = _task(client)
    finding = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("findings.md", b"# Findings\n\nConfirmed IDOR", "text/markdown")},
        data={"actor": "human", "summary": "Findings"},
    ).json()
    text_evidence = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("response.txt", b"HTTP/1.1 200 OK", "text/plain")},
        data={"actor": "human", "summary": "Response"},
    ).json()
    binary_evidence = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("proof.png", b"\x89PNG\r\n\x1a\n\x00\xff", "image/png")},
        data={"actor": "human", "summary": "Screenshot"},
    ).json()
    vulnerability = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "越权读取",
            "type": "越权访问",
            "severity": "high",
            "description": "普通用户读取了其他用户资源",
            "impact": "造成数据泄露",
            "source": "human",
            "reported_by": "human",
            "finding_artifact_id": finding["id"],
            "evidence_artifact_ids": [text_evidence["id"], binary_evidence["id"]],
        },
    ).json()
    _configure(client)

    preview = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-preview"
    ).json()["payload"]

    assert preview["schema_version"] == 2
    assert preview["finding"]["content"].startswith("# Findings")
    assert preview["evidence"][0]["content"] == "HTTP/1.1 200 OK"
    assert preview["evidence"][1]["name"] == "proof.png"
    assert preview["evidence"][1]["content"] is None


def test_external_report_persists_success_and_reuses_common_payload(client) -> None:
    task = _task(client)
    vulnerability = _vulnerability(client, task["id"])
    _configure(client)
    container = client.app.state.container
    calls: list[tuple[str, dict, dict]] = []

    def send(endpoint, payload, headers, _config):
        calls.append((endpoint, payload, headers))
        return 202, '{"accepted":true}'

    container.external_reports.sender = send
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-report"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] is True
    assert result["attempt"]["attempt_no"] == 1
    assert result["attempt"]["status"] == "succeeded"
    assert result["attempt"]["payload"]["title"] == "越权读取"
    assert calls[0][0].endswith("/vulnerabilities")
    assert calls[0][1]["title"] == "越权读取"
    assert calls[0][2]["Authorization"] == "Bearer external-secret"

    attempts = client.get(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-attempts"
    )
    assert attempts.status_code == 200
    assert [item["attempt_no"] for item in attempts.json()] == [1]


def test_failed_report_can_retry_until_configured_limit(client) -> None:
    task = _task(client)
    vulnerability = _vulnerability(client, task["id"])
    _configure(client, max_attempts=2)
    container = client.app.state.container
    calls = 0

    def send(_endpoint, _payload, _headers, _config):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 503, "temporarily unavailable"
        return 200, "ok"

    container.external_reports.sender = send
    path = f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}"
    first = client.post(f"{path}/external-report")
    assert first.status_code == 200
    assert first.json()["accepted"] is False
    assert first.json()["attempt"]["error"] == "External endpoint returned HTTP 503"

    retried = client.post(f"{path}/external-report/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["accepted"] is True
    assert retried.json()["attempt"]["attempt_no"] == 2

    exhausted = client.post(f"{path}/external-report/retry")
    assert exhausted.status_code == 409


def test_external_report_requires_task_switch(client) -> None:
    task = _task(client, enabled=False)
    vulnerability = _vulnerability(client, task["id"])
    _configure(client)
    response = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}/external-report"
    )
    assert response.status_code == 409
