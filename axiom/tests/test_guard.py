from __future__ import annotations

import pytest

from axiom.modules.guard.defaults import HARD_BLOCK_RULES, SYSTEM_PROMPT
from axiom.modules.guard.policy import evaluate
from axiom.modules.guard.policy import _external_operation
from axiom.modules.guard.schemas import GuardRequest
from axiom.modules.settings.schemas import RuntimeConfig


def _task(client, *, enabled: bool) -> dict:
    project = client.post("/api/v1/projects", json={"title": "Guard"}).json()
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "监管",
            "origin": "a.example.com",
            "guard_enabled": enabled,
        },
    ).json()


def _mode(client, mode: str) -> None:
    current = client.get("/api/v1/settings/runtime").json()
    config = current["config"]
    config["guard_mode"] = mode
    response = client.put(
        "/api/v1/settings/runtime",
        json={"actor": "test", "expected_version": current["version"], "config": config},
    )
    assert response.status_code == 200, response.text


def _update_guard(client, **changes) -> dict:
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


def _decide(client, task: dict, command: str) -> dict:
    response = client.post(
        "/api/v1/internal/guard/pre",
        json={
            "project_id": task["project_id"],
            "task_id": task["id"],
            "intent_id": "i001",
            "phase": "bootstrap",
            "worker": "worker-a",
            "tool_name": "bash",
            "input": {"command": command},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_guard_is_task_scoped_and_hard_rules_block_only_when_enabled(client) -> None:
    disabled = _task(client, enabled=False)
    assert _decide(client, disabled, "systemctl restart nginx")["source"] == "disabled"

    enabled = _task(client, enabled=True)
    decision = _decide(client, enabled, "systemctl restart nginx")
    assert decision["decision"] == "BLOCK"
    assert decision["source"] == "hard_rule"
    assert decision["event_id"].startswith("evt_")

    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": enabled["id"], "event_type": "guard.decision"},
    ).json()
    assert events[0]["payload"]["decision"] == "BLOCK"


def test_guard_audit_mode_records_block_but_does_not_interrupt_worker(client) -> None:
    task = _task(client, enabled=True)
    _mode(client, "audit")

    decision = _decide(client, task, "rm -rf /etc")

    assert decision["decision"] == "ALLOW"
    assert decision["observed_decision"] == "BLOCK"
    assert decision["observed_reason"]


def test_guard_audit_preserves_review_run_owner(client) -> None:
    task = _task(client, enabled=False)
    response = client.post(
        "/api/v1/internal/guard/pre",
        json={
            "project_id": task["project_id"],
            "task_id": task["id"],
            "review_run_id": "vr000001",
            "phase": "review",
            "worker": "reviewer-a",
            "tool_name": "read",
            "input": {"path": "review_target.json"},
        },
    )
    assert response.status_code == 200, response.text

    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": task["id"], "event_type": "guard.decision"},
    ).json()
    assert events[0]["review_run_id"] == "vr000001"
    assert events[0]["payload"]["phase"] == "review"


def test_guard_fails_open_for_normal_external_operation_without_model_config(client) -> None:
    task = _task(client, enabled=True)
    decision = _decide(client, task, "curl https://a.example.com/health")
    assert decision["decision"] == "ALLOW"
    assert decision["source"] == "fail_open"


def test_guard_policy_fields_are_persisted_and_drive_the_next_decision(client) -> None:
    task = _task(client, enabled=True)
    updated = _update_guard(
        client,
        guard_whitelist_tools=["read", "custom-safe"],
        guard_whitelist_bash_verbs=["echo"],
        guard_hard_block_rules=[
            {"reason": "禁止触碰标记文件", "pattern": r"touch\s+/tmp/blocked", "enabled": True}
        ],
        guard_system_prompt="仅按当前授权范围判断。",
    )

    assert updated["config"]["guard_whitelist_bash_verbs"] == ["echo"]
    assert updated["config"]["guard_system_prompt"] == "仅按当前授权范围判断。"
    assert _decide(client, task, "echo ok")["source"] == "whitelist"
    blocked = _decide(client, task, "touch /tmp/blocked")
    assert blocked["decision"] == "BLOCK"
    assert blocked["reason"] == "禁止触碰标记文件"
    # The removed default rule must no longer be silently applied.
    assert _decide(client, task, "systemctl restart nginx")["decision"] == "ALLOW"


def test_guard_rejects_an_invalid_configured_regex(client) -> None:
    current = client.get("/api/v1/settings/runtime").json()
    response = client.put(
        "/api/v1/settings/runtime",
        json={
            "actor": "test",
            "expected_version": current["version"],
            "config": {
                **current["config"],
                "guard_hard_block_rules": [
                    {"reason": "坏规则", "pattern": "(", "enabled": True}
                ],
            },
        },
    )

    assert response.status_code == 422
    assert "invalid guard regex" in response.text


def test_guard_treats_native_browser_tools_as_external_operations() -> None:
    assert _external_operation({"tool_name": "browser_click", "command": "{}"})
    assert _external_operation({"tool_name": "browser_read_network", "command": "{}"})


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("passwd alice", "修改或重置用户密码"),
        ("systemctl restart nginx", "停止或重启业务服务"),
        ("crontab -e", "修改系统或服务配置"),
        ('echo "x" > /etc/nginx/conf.d/test.conf', "修改系统或服务配置"),
        ("userdel alice", "篡改账户或权限体系"),
        ("rm -rf /etc", "删除或大规模破坏关键数据"),
        ("psql -c \"DROP TABLE users\"", "删除或大规模破坏关键数据"),
        ("ab -n 100000 https://target.example", "高并发或 DoS 操作"),
    ],
)
def test_default_hard_rules_block_explicit_destructive_commands(command: str, reason: str) -> None:
    config = RuntimeConfig(guard_mode="enforce")
    decision = evaluate(
        GuardRequest(tool_name="bash", input={"command": command}),
        config,
    )
    assert decision.decision == "BLOCK"
    assert decision.source == "hard_rule"
    assert decision.reason == reason


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "curl https://target.example/api -d 'reset_password=true'",
        "curl https://target.example/api -d \"DROP TABLE users\"",
        "curl https://target.example/upload -F file=@webshell.php",
        "hydra -l admin -P passwords.txt target.example http-post-form",
        "ffuf -w paths.txt -u https://target.example/FUZZ",
        "nmap -sV target.example",
    ],
)
def test_default_hard_rules_do_not_block_normal_security_testing(command: str) -> None:
    config = RuntimeConfig(guard_mode="enforce")
    decision = evaluate(
        GuardRequest(tool_name="bash", input={"command": command}),
        config,
    )
    assert decision.decision == "ALLOW"


def test_default_judge_prompt_preserves_allow_first_contract() -> None:
    assert "默认倾向 ALLOW" in SYSTEM_PROMPT
    assert "只回复一行" in SYSTEM_PROMPT
    assert "BLOCK:<不超过30字" in SYSTEM_PROMPT
    assert "SQL 注入" in SYSTEM_PROMPT
    assert "上传 WebShell" in SYSTEM_PROMPT
    assert "大规模压测" in SYSTEM_PROMPT
    assert len(HARD_BLOCK_RULES) == 6
