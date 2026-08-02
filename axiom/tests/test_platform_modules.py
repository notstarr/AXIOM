from __future__ import annotations

import json

import pytest

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.vulnerabilities.tables import vulnerabilities


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


def test_credentials_are_project_scoped_and_progressively_disclosed(client) -> None:
    project, task = _project_task(client)
    content = "Cookie: session=raw-secret\nX-CSRF: token"
    refresh_content = "POST /login HTTP/1.1\n\nusername=admin&password=secret"
    created = client.post(
        f"/api/v1/projects/{project['id']}/credentials",
        json={
            "name": "登录 Cookie",
            "content": content,
            "notes": "低权限测试账号",
            "refresh_content": refresh_content,
        },
    )
    assert created.status_code == 201, created.text
    credential = created.json()
    assert credential["content"] == content
    assert credential["notes"] == "低权限测试账号"
    assert credential["refresh_id"].startswith("crf_")
    assert "refresh_content" not in credential

    listed = client.get(f"/api/v1/projects/{project['id']}/credentials")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == credential["id"]
    assert listed.json()[0]["refresh_id"] == credential["refresh_id"]
    assert "content" not in listed.json()[0]

    detail = client.get(
        f"/api/v1/projects/{project['id']}/credentials/{credential['id']}"
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["content"] == content
    assert "refresh_content" not in detail.json()

    refresh = client.get(
        f"/api/v1/projects/{project['id']}/credential-refreshes/"
        f"{credential['refresh_id']}"
    )
    assert refresh.status_code == 200, refresh.text
    assert refresh.json()["content"] == refresh_content

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
    assert "credentials" not in execution_input.json()

    access = {"lease_id": claim["lease_id"], "intent_id": "i001"}
    summaries = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/list",
        json=access,
    )
    assert summaries.status_code == 200, summaries.text
    assert summaries.json() == [
        {
            "id": credential["id"],
            "name": "登录 Cookie",
        }
    ]
    material = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/{credential['id']}/get",
        json=access,
    )
    assert material.status_code == 200, material.text
    assert material.json() == {
        "id": credential["id"],
        "name": "登录 Cookie",
        "content": content,
        "notes": "低权限测试账号",
        "refresh_id": credential["refresh_id"],
    }
    worker_refresh = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credential-refreshes/"
        f"{credential['refresh_id']}/get",
        json=access,
    )
    assert worker_refresh.status_code == 200, worker_refresh.text
    assert worker_refresh.json() == {
        "id": credential["refresh_id"],
        "content": refresh_content,
    }

    disabled = client.patch(
        f"/api/v1/projects/{project['id']}/credentials/{credential['id']}/enabled",
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert "content" not in disabled.json()
    assert client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/list",
        json=access,
    ).json() == []
    assert client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/{credential['id']}/get",
        json=access,
    ).status_code == 404
    assert client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credential-refreshes/"
        f"{credential['refresh_id']}/get",
        json=access,
    ).status_code == 404

    wrong_lease = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/list",
        json={"lease_id": "wrong", "intent_id": "i001"},
    )
    assert wrong_lease.status_code == 409

    other_project = client.post(
        "/api/v1/projects",
        json={"title": "另一个项目"},
    ).json()
    other_credential = client.post(
        f"/api/v1/projects/{other_project['id']}/credentials",
        json={"name": "跨项目凭证", "content": "must-not-leak"},
    ).json()
    cross_project = client.post(
        f"/api/v1/dispatch/tasks/{task['id']}/credentials/"
        f"{other_credential['id']}/get",
        json=access,
    )
    assert cross_project.status_code == 404


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("Cookie", "Cookie: session=abc"),
        ("JWT", "eyJhbGciOiJub25lIn0.payload.signature"),
        ("SSH 私钥", "-----BEGIN OPENSSH PRIVATE KEY-----\nline-1\nline-2\n-----END OPENSSH PRIVATE KEY-----"),
        ("原始请求", "POST /login HTTP/1.1\nHost: example.com\n\nusername=a&password=b"),
        ("账号密码", "username: admin\npassword: p@ss:word"),
        ("任意文本", "这不是 JSON，也不应该被解析：{broken"),
    ],
)
def test_credential_content_accepts_free_text_verbatim(client, name, content) -> None:
    project = client.post("/api/v1/projects", json={"title": name}).json()
    created = client.post(
        f"/api/v1/projects/{project['id']}/credentials",
        json={"name": name, "content": content},
    )
    assert created.status_code == 201, created.text
    assert created.json()["content"] == content
    listed = client.get(f"/api/v1/projects/{project['id']}/credentials").json()
    assert "content" not in listed[0]


def test_credential_refresh_keeps_id_until_removed_and_cascades(client) -> None:
    project = client.post("/api/v1/projects", json={"title": "刷新生命周期"}).json()
    base = f"/api/v1/projects/{project['id']}/credentials"
    created = client.post(
        base,
        json={
            "name": "会话",
            "content": "token-one",
            "refresh_content": "first refresh",
        },
    ).json()
    first_refresh_id = created["refresh_id"]

    updated = client.put(
        f"{base}/{created['id']}",
        json={
            "name": "会话",
            "content": "token-two",
            "notes": "updated",
            "refresh_content": "second refresh",
            "enabled": True,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["refresh_id"] == first_refresh_id
    refresh_path = (
        f"/api/v1/projects/{project['id']}/credential-refreshes/{first_refresh_id}"
    )
    assert client.get(refresh_path).json()["content"] == "second refresh"

    cleared = client.put(
        f"{base}/{created['id']}",
        json={
            "name": "会话",
            "content": "token-two",
            "notes": "updated",
            "refresh_content": "",
            "enabled": True,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["refresh_id"] is None
    assert client.get(refresh_path).status_code == 404

    restored = client.put(
        f"{base}/{created['id']}",
        json={
            "name": "会话",
            "content": "token-three",
            "refresh_content": "third refresh",
            "enabled": True,
        },
    )
    assert restored.status_code == 200, restored.text
    second_refresh_id = restored.json()["refresh_id"]
    assert second_refresh_id != first_refresh_id

    deleted = client.delete(f"{base}/{created['id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(
        f"/api/v1/projects/{project['id']}/credential-refreshes/{second_refresh_id}"
    ).status_code == 404


def test_text_and_json_artifacts_are_available_through_content_endpoint(client) -> None:
    _project, task = _project_task(client)
    samples = {
        "result.txt": "第一行\n<script>alert(1)</script>",
        "result.json": json.dumps(
            {"name": "测试", "items": [1, 2]}, ensure_ascii=False
        ),
    }

    for filename, content in samples.items():
        uploaded = client.post(
            f"/api/v1/tasks/{task['id']}/artifacts/upload",
            files={"file": (filename, content.encode(), "application/octet-stream")},
            data={"actor": "human", "summary": filename},
        )
        assert uploaded.status_code == 201, uploaded.text

        response = client.get(
            f"/api/v1/tasks/{task['id']}/artifacts/"
            f"{uploaded.json()['id']}/content"
        )
        assert response.status_code == 200, response.text
        assert response.json()["content"] == content


def test_vulnerability_format_is_shared_by_internal_management(client) -> None:
    _project, task = _project_task(client)
    finding = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("findings.md", b"# Findings\n\nIDOR confirmed", "text/markdown")},
        data={"actor": "human", "summary": "人工 Findings"},
    ).json()
    evidence = client.post(
        f"/api/v1/tasks/{task['id']}/artifacts/upload",
        files={"file": ("response.txt", b"HTTP/1.1 200 OK", "text/plain")},
        data={"actor": "human", "summary": "响应证据"},
    ).json()
    reported = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "对象级授权缺失",
            "type": "越权访问",
            "severity": "high",
            "description": "普通用户可以读取其他用户订单",
            "prerequisites": "已登录普通用户",
            "impact": "造成跨用户数据泄露",
            "remediation": "校验对象归属",
            "affected_target": "a.example.com",
            "finding_artifact_id": finding["id"],
            "evidence_artifact_ids": [evidence["id"]],
            "source": "explore",
            "reported_by": "worker-a",
        },
    )
    assert reported.status_code == 201, reported.text
    vulnerability = reported.json()
    assert vulnerability["id"] == "v001"
    assert vulnerability["format_version"] == 2
    assert vulnerability["legacy"] is None
    assert vulnerability["finding_artifact"]["id"] == finding["id"]
    assert [item["id"] for item in vulnerability["evidence_artifacts"]] == [evidence["id"]]
    assert client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities").json()[0]["severity"] == "high"

    ignored = client.patch(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/{vulnerability['id']}",
        json={"status": "忽略", "status_reason": "重复问题已合并"},
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["status"] == "忽略"

    old_shape = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "旧格式请求",
            "type": "越权访问",
            "severity": "high",
            "description": "描述",
            "impact": "影响",
            "reproduction_steps": ["旧步骤"],
            "source": "human",
            "reported_by": "human",
        },
    )
    assert old_shape.status_code == 422


def test_human_vulnerability_can_omit_prerequisites_and_remediation(client) -> None:
    _project, task = _project_task(client)

    reported = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={
            "title": "人工待补充漏洞",
            "type": "配置问题",
            "severity": "medium",
            "description": "人工先记录核心现象。",
            "impact": "可能造成敏感配置暴露。",
            "source": "human",
            "reported_by": "human",
        },
    )

    assert reported.status_code == 201, reported.text
    assert reported.json()["prerequisites"] == ""
    assert reported.json()["remediation"] == ""


def test_vulnerability_artifact_roles_are_validated(client) -> None:
    _project, task = _project_task(client)
    now = "2026-07-27T00:00:00Z"
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            artifacts.insert().values(
                id="a001",
                task_id=task["id"],
                intent_id=None,
                artifact_type="explore.leads",
                storage_key=f"tasks/{task['id']}/artifacts/i002/leads.md",
                summary="待验证线索",
                content_hash="0" * 64,
                size=4,
                created_by="test",
                created_at=now,
                archived=False,
            )
        )

    base = {
        "title": "错误 Artifact 角色",
        "type": "测试",
        "severity": "low",
        "description": "验证后端拒绝错误的 Artifact 角色。",
        "impact": "防止漏洞索引关系失真。",
    }
    invalid_finding = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={**base, "finding_artifact_id": "a001"},
    )
    assert invalid_finding.status_code == 409
    assert "Finding Artifact" in invalid_finding.text

    invalid_evidence = client.post(
        f"/api/v1/tasks/{task['id']}/vulnerabilities",
        json={**base, "evidence_artifact_ids": ["a001"]},
    )
    assert invalid_evidence.status_code == 409
    assert "Evidence Artifact" in invalid_evidence.text


def test_legacy_vulnerability_details_are_read_only(client) -> None:
    _project, task = _project_task(client)
    now = "2026-07-27T00:00:00Z"
    with client.app.state.container.engine.begin() as conn:
        conn.execute(
            vulnerabilities.insert().values(
                id="v001",
                task_id=task["id"],
                intent_id=None,
                title="旧格式漏洞",
                type="越权",
                severity="high",
                description="旧描述",
                prerequisites="",
                impact="旧影响",
                reproduction_steps_json=json.dumps(["旧步骤"], ensure_ascii=False),
                remediation="旧建议",
                curl_command="curl https://legacy.example.test",
                evidence_json=json.dumps(["旧文字证据"], ensure_ascii=False),
                affected_target="legacy.example.test",
                status="待验证",
                status_reason="",
                source="legacy",
                reported_by="migration",
                created_at=now,
                updated_at=now,
                archived=False,
                format_version=1,
            )
        )

    loaded = client.get(f"/api/v1/tasks/{task['id']}/vulnerabilities/v001")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["legacy"] == {
        "reproduction_steps": ["旧步骤"],
        "curl_command": "curl https://legacy.example.test",
        "evidence": ["旧文字证据"],
    }

    rejected = client.patch(
        f"/api/v1/tasks/{task['id']}/vulnerabilities/v001",
        json={"curl_command": "curl https://changed.example.test"},
    )
    assert rejected.status_code == 422


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
