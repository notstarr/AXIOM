from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from axiom.dispatcher.config import WorkerConfig
from axiom.dispatcher.tasks.common import worker_with_project_runtime
from axiom.dispatcher.workers.adapters.pi import AXIOM_GUARD_EXTENSION_INDEX, PiDriver
from axiom.guard.policy import GuardPolicyConfig, evaluate_guard_event
from axiom.server.judge_settings import DEFAULT_JUDGE_SYSTEM_PROMPT
from axiom.server import db
from axiom.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setenv("MASTER_KEY", "test-master-key")
    db.configure(tmp_path / "axiom.db")
    with TestClient(app) as test_client:
        yield test_client


def test_guard_hard_rule_blocks_destructive_service_restart(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    result = evaluate_guard_event(tool_name="bash", input_payload={"command": "systemctl restart nginx"})
    assert result.decision == "BLOCK"
    assert result.source == "hard_rule"
    assert "服务" in result.reason


def test_guard_whitelist_allows_safe_tools_and_bash(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    read_result = evaluate_guard_event(tool_name="read", input_payload={"path": "/tmp/a.txt"})
    assert read_result.decision == "ALLOW"
    assert read_result.source == "whitelist"

    bash_result = evaluate_guard_event(tool_name="bash", input_payload={"command": "whoami && id && uname -a"})
    assert bash_result.decision == "ALLOW"
    assert bash_result.source == "whitelist"


def test_guard_does_not_blanket_block_http_delete_method(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    for command in [
        "curl -X DELETE https://target.test/api/users/123",
        "curl --request DELETE https://target.test/api/files/1",
        "http DELETE https://target.test/api/users/123",
        "axios.delete('https://target.test/api/users/123')",
        "fetch(url, { method: 'DELETE' })",
    ]:
        result = evaluate_guard_event(tool_name="bash", input_payload={"command": command})
        assert result.decision == "ALLOW"
        assert result.source != "hard_rule"


def test_guard_hard_rules_ignore_report_heredoc_body(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    command = """cat > /tmp/axiom-files/pg_008/proj_077/ai/i006_findings.md << 'FINDINGS_EOF'
# 测试结果

| DELETE /inventory/blacklist/delete | HTTP DELETE 方法禁用 |

```bash
curl -X DELETE https://target.test/api/users/123
systemctl restart nginx
```
FINDINGS_EOF
echo "Done"""
    result = evaluate_guard_event(tool_name="bash", input_payload={"command": command})
    assert result.decision == "ALLOW"
    assert result.source != "hard_rule"


def test_guard_allows_minimal_http_delete_after_heredoc(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    command = """cat > /tmp/report.md <<EOF
HTTP DELETE 方法禁用
EOF
curl -X DELETE https://target.test/api/users/123"""
    result = evaluate_guard_event(tool_name="bash", input_payload={"command": command})
    assert result.decision == "ALLOW"
    assert result.source != "hard_rule"


def test_guard_defaults_allow_for_normal_pentest_payload(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    result = evaluate_guard_event(
        tool_name="bash",
        input_payload={"command": "sqlmap -u 'https://example.test/item?id=1' --batch --risk=1"},
    )
    assert result.decision == "ALLOW"


def test_guard_model_block_and_timeout_fail_open(monkeypatch) -> None:
    import requests

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "BLOCK:测试阻断"}}]}

    captured = {}
    def fake_post(*_args, **kwargs):
        captured.update(kwargs.get("json") or {})
        return FakeResponse()

    monkeypatch.setattr("axiom.guard.policy.requests.post", fake_post)
    result = evaluate_guard_event(tool_name="bash", input_payload={"command": "curl https://example.test/danger"}, api_key="sk-test")
    assert result.decision == "BLOCK"
    assert result.source == "model"
    assert result.reason == "测试阻断"
    assert captured["enable_thinking"] is False
    assert "最小化验证" in captured["messages"][0]["content"]

    def timeout_post(*_args, **_kwargs):
        raise requests.ReadTimeout("slow")

    monkeypatch.setattr("axiom.guard.policy.requests.post", timeout_post)
    timeout_result = evaluate_guard_event(tool_name="bash", input_payload={"command": "curl https://example.test/slow"}, api_key="sk-test")
    assert timeout_result.decision == "ALLOW"
    assert timeout_result.source == "fail_open"


def test_judge_settings_defaults_and_key_storage(client: TestClient) -> None:
    payload = client.get("/settings/judge").json()
    assert payload["enabled_default"] is False
    assert payload["mode"] == "enforce"
    assert payload["base_url"] == "https://api.siliconflow.cn/v1"
    assert payload["model"] == "Qwen/Qwen3.5-9B"
    assert payload["timeout_seconds"] == 3
    assert payload["whitelist_tools"] == ["read", "ls", "grep", "find"]
    assert payload["has_key"] is False

    saved = client.put("/settings/keys/judge", json={"api_key": "sk-test-secret", "updated_by": "pytest"})
    assert saved.status_code == 200
    assert saved.json()["masked_key"] == "****cret"
    refreshed = client.get("/settings/judge").json()
    assert refreshed["has_key"] is True
    assert refreshed["masked_key"] == "****cret"
    assert "最小化验证" in refreshed["system_prompt"]
    assert "【测试规范边界】" not in refreshed["system_prompt"]
    assert "注入测试中读取表内真实数据" in refreshed["system_prompt"]
    assert "注入测试只证明可读" in refreshed["system_prompt"]
    assert refreshed["default_system_prompt"] == DEFAULT_JUDGE_SYSTEM_PROMPT
    assert any(rule["reason"] == "删除或清空关键数据" for rule in refreshed["hard_block_rules"])
    assert refreshed["default_hard_block_rules"]


def test_judge_settings_custom_prompt_and_validation(client: TestClient) -> None:
    payload = client.get("/settings/judge").json()
    custom_prompt = "自定义裁判提示词：只回复 ALLOW 或 BLOCK:原因"
    payload.update({"system_prompt": custom_prompt})
    saved = client.put("/settings/judge", json=payload)
    assert saved.status_code == 200
    assert saved.json()["system_prompt"] == custom_prompt
    assert client.get("/settings/judge").json()["system_prompt"] == custom_prompt

    payload.update({"system_prompt": "   "})
    invalid = client.put("/settings/judge", json=payload)
    assert invalid.status_code == 422


def test_judge_settings_custom_hard_rules(client: TestClient) -> None:
    payload = client.get("/settings/judge").json()
    payload.update({"hard_block_rules": [{"reason": "自定义阻断", "pattern": "CUSTOM_FORBIDDEN", "enabled": True}]})
    saved = client.put("/settings/judge", json=payload)
    assert saved.status_code == 200
    assert saved.json()["hard_block_rules"] == [{"reason": "自定义阻断", "pattern": "CUSTOM_FORBIDDEN", "enabled": True}]

    response = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": "proj_test",
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "echo CUSTOM_FORBIDDEN"},
            "mode": "enforce",
        },
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "BLOCK"
    assert response.json()["reason"] == "🤖裁判模型提醒:自定义阻断"

    payload.update({"hard_block_rules": [{"reason": "坏正则", "pattern": "(", "enabled": True}]})
    invalid = client.put("/settings/judge", json=payload)
    assert invalid.status_code == 422


def test_guard_model_uses_custom_system_prompt(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": "ALLOW"}}]}

    captured = {}
    def fake_post(*_args, **kwargs):
        captured.update(kwargs.get("json") or {})
        return FakeResponse()

    monkeypatch.setattr("axiom.guard.policy.requests.post", fake_post)
    result = evaluate_guard_event(
        tool_name="bash",
        input_payload={"command": "curl https://example.test/probe"},
        config=GuardPolicyConfig(system_prompt="CUSTOM SYSTEM PROMPT"),
        api_key="sk-test",
    )
    assert result.decision == "ALLOW"
    assert captured["messages"][0]["content"] == "CUSTOM SYSTEM PROMPT"
    assert captured["enable_thinking"] is False


def test_judge_test_uses_saved_system_prompt(client: TestClient, monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": "ALLOW"}}]}

    captured = {}
    def fake_post(*_args, **kwargs):
        captured.update(kwargs.get("json") or {})
        return FakeResponse()

    client.put("/settings/keys/judge", json={"api_key": "sk-test-secret", "updated_by": "pytest"})
    payload = client.get("/settings/judge").json()
    payload.update({"system_prompt": "JUDGE TEST PROMPT"})
    assert client.put("/settings/judge", json=payload).status_code == 200
    monkeypatch.setattr("axiom.server.routers.settings.requests.post", fake_post)

    response = client.post("/settings/judge/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["messages"][0]["content"] == "JUDGE TEST PROMPT"
    assert captured["enable_thinking"] is False


def test_judge_key_save_missing_master_key_returns_json_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.delenv("MASTER_KEY", raising=False)
    monkeypatch.setenv("CAIRN_MASTER_KEY_FILE", str(tmp_path / "missing-master.key"))
    db.configure(tmp_path / "axiom.db")
    with TestClient(app) as test_client:
        response = test_client.put(
            "/settings/keys/judge",
            json={"api_key": "sk-test-secret", "updated_by": "pytest"},
        )
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert "MASTER_KEY is not configured" in response.json()["detail"]


def test_judge_key_save_uses_persisted_master_key_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.delenv("MASTER_KEY", raising=False)
    master_key_file = tmp_path / "master.key"
    master_key_file.write_text("file-backed-test-master-key", encoding="utf-8")
    monkeypatch.setenv("CAIRN_MASTER_KEY_FILE", str(master_key_file))
    db.configure(tmp_path / "axiom.db")
    with TestClient(app) as test_client:
        response = test_client.put(
            "/settings/keys/judge",
            json={"api_key": "sk-test-secret", "updated_by": "pytest"},
        )
    assert response.status_code == 200
    assert response.json()["masked_key"] == "****cret"


def test_project_guard_enabled_defaults_and_persists(client: TestClient) -> None:
    base = {
        "title": "guard project",
        "origin": "start",
        "goal": "finish",
    }
    created = client.post("/projects", json=base)
    assert created.status_code == 201
    assert created.json()["project"]["guard_enabled"] is False

    created_guard = client.post("/projects", json={**base, "title": "guard on", "guard_enabled": True})
    assert created_guard.status_code == 201
    project_id = created_guard.json()["project"]["id"]
    assert created_guard.json()["project"]["guard_enabled"] is True
    listed = client.get("/projects").json()
    assert next(item for item in listed if item["id"] == project_id)["guard_enabled"] is True


def test_task_scope_blocks_out_of_scope_agent_tool_when_guard_is_off(client: TestClient) -> None:
    created = client.post(
        "/projects",
        json={
            "title": "scope hook",
            "origin": "https://api.example.com",
            "goal": "finish",
            "guard_enabled": False,
            "task_scope": {
                "enabled": True,
                "targets": ["*.example.com"],
                "exclude": ["blocked.example.com"],
            },
        },
    )
    assert created.status_code == 201
    project_id = created.json()["project"]["id"]

    blocked = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": project_id,
            "phase": "bootstrap",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "curl -sS https://outside.test/api"},
            "mode": "off",
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["decision"] == "BLOCK"
    assert blocked.json()["source"] == "task_scope"
    assert "outside.test" in blocked.json()["reason"]

    denied = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": project_id,
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "nmap blocked.example.com"},
            "mode": "off",
        },
    )
    assert denied.status_code == 200
    assert denied.json()["decision"] == "BLOCK"
    assert denied.json()["source"] == "task_scope"

    for command in [
        "nc -zv outside.test 443",
        "echo >/dev/tcp/outside.test/443",
    ]:
        socket_blocked = client.post(
            "/tool-hooks/pre",
            json={
                "project_id": project_id,
                "phase": "explore_execute",
                "worker": "w1",
                "tool_name": "bash",
                "input": {"command": command},
                "mode": "off",
            },
        )
        assert socket_blocked.status_code == 200
        assert socket_blocked.json()["decision"] == "BLOCK"
        assert socket_blocked.json()["source"] == "task_scope"

    allowed = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": project_id,
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "curl -sS https://api.example.com/health"},
            "mode": "off",
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["decision"] == "ALLOW"

    report_only = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": project_id,
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {
                "command": "cat > /tmp/report.md <<'EOF'\nObserved https://outside.test but did not test it.\nEOF"
            },
            "mode": "off",
        },
    )
    assert report_only.status_code == 200
    assert report_only.json()["decision"] == "ALLOW"


def test_tool_hook_observe_records_but_does_not_enforce(client: TestClient) -> None:
    response = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": "proj_test",
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "systemctl restart nginx"},
            "mode": "observe",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"] == "ALLOW"
    assert payload["observed_decision"] == "BLOCK"
    assert payload["enforced"] is False
    events = client.get("/tool-hooks/events?project_id=proj_test").json()
    assert len(events) == 1
    assert events[0]["observed_decision"] == "BLOCK"


def test_tool_hook_guarded_enforces_hard_rule(client: TestClient) -> None:
    response = client.post(
        "/tool-hooks/pre",
        json={
            "project_id": "proj_test",
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "rm -rf /var/www"},
            "mode": "guarded",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["decision"] == "BLOCK"
    assert payload["source"] == "hard_rule"
    assert payload["enforced"] is True


def test_tool_hook_events_filter_by_decision_and_project(client: TestClient) -> None:
    client.post(
        "/tool-hooks/pre",
        json={
            "project_id": "proj_filter_a",
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "whoami"},
            "mode": "enforce",
        },
    )
    client.post(
        "/tool-hooks/pre",
        json={
            "project_id": "proj_filter_a",
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "rm -rf /var/www"},
            "mode": "enforce",
        },
    )
    client.post(
        "/tool-hooks/pre",
        json={
            "project_id": "proj_filter_b",
            "phase": "explore_execute",
            "worker": "w1",
            "tool_name": "bash",
            "input": {"command": "systemctl restart nginx"},
            "mode": "enforce",
        },
    )

    blocked = client.get("/tool-hooks/events?decision=block").json()
    assert blocked
    assert all(event["decision"] == "BLOCK" for event in blocked)

    allowed = client.get("/tool-hooks/events?decision=allow").json()
    assert allowed
    assert all(event["decision"] == "ALLOW" for event in allowed)

    project_blocked = client.get("/tool-hooks/events?project_id=proj_filter_a&decision=block").json()
    assert len(project_blocked) == 1
    assert project_blocked[0]["project_id"] == "proj_filter_a"
    assert project_blocked[0]["decision"] == "BLOCK"


def test_pi_driver_loads_guard_extension_only_for_guarded_bootstrap_explore(monkeypatch) -> None:
    monkeypatch.delenv("AXIOM_GUARD_ENABLED", raising=False)
    worker = WorkerConfig.model_validate(
        {
            "name": "guard-test-worker",
            "type": "pi",
            "env": {
                "PI_MODEL": "test-model",
                "PI_BASE_URL": "http://localhost:1234",
                "PI_API_KEY": "test-key",
                "PI_PROVIDER_API": "responses",
            },
            "task_types": ["bootstrap", "explore", "reason"],
            "max_running": 1,
            "priority": 0,
        }
    )
    driver = PiDriver()
    old_argv = driver.build_execute(worker, "say hi", None, phase="explore").argv
    assert "--no-extensions" in old_argv

    guarded_worker = worker_with_project_runtime(worker, guard_enabled=True)
    argv = driver.build_execute(guarded_worker, "say hi", None, phase="explore").argv
    assert "--no-extensions" not in argv
    settings_path = "/tmp/axiom-pi/guard-test-worker/settings.json"
    with open(settings_path, "r", encoding="utf-8") as fh:
        settings = json.load(fh)
    assert AXIOM_GUARD_EXTENSION_INDEX in settings["extensions"]

    reason_argv = driver.build_execute(guarded_worker, "say hi", None, phase="reason").argv
    assert "--no-extensions" in reason_argv

    scoped_worker = worker_with_project_runtime(
        worker,
        guard_enabled=False,
        task_scope_enabled=True,
        credential_headers="Authorization: Bearer task-secret",
    )
    scoped_argv = driver.build_execute(scoped_worker, "say hi", None, phase="bootstrap").argv
    assert "--no-extensions" not in scoped_argv
    assert scoped_worker.env["AXIOM_TASK_SCOPE_ENABLED"] == "1"
    assert scoped_worker.env["AXIOM_REQUEST_HEADERS"] == "Authorization: Bearer task-secret"
    assert all("task-secret" not in token for token in scoped_argv)
