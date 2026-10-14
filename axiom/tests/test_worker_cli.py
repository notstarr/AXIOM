from __future__ import annotations

import json
import stat
from pathlib import Path

from click.testing import CliRunner

from axiom.cli import main


def _env() -> dict[str, str]:
    return {
        "AXIOM_SERVER": "http://127.0.0.1:8899",
        "AXIOM_TASK_ID": "t_001",
        "AXIOM_INTENT_ID": "i003",
        "AXIOM_REASON_RUN_ID": "rr000001",
        "AXIOM_LEASE_ID": "lease-1",
        "AXIOM_REASON_WORKSPACE_ID": "rw_000001",
    }


def _review_env() -> dict[str, str]:
    environment = _env()
    environment.pop("AXIOM_INTENT_ID")
    environment.pop("AXIOM_REASON_RUN_ID")
    environment.pop("AXIOM_REASON_WORKSPACE_ID")
    environment["AXIOM_REVIEW_RUN_ID"] = "vr000001"
    environment["AXIOM_VULNERABILITY_ID"] = "v001"
    return environment


def test_reason_cli_sends_one_small_todo_operation_with_repeatable_sources(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "rw_000001", "status": "open", "draft": {}}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "reason",
            "todo",
            "add",
            "--ref",
            "authz",
            "--title",
            "验证对象级授权",
            "--source",
            "fact:f001",
            "--source",
            "artifact:a003",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "/reason-workspaces/rw_000001/todos",
            {
                "lease_id": "lease-1",
                "todo": {
                    "ref": "authz",
                    "title": "验证对象级授权",
                    "status": "open",
                    "status_reason": "",
                    "sources": [
                        {"type": "fact", "id": "f001"},
                        {"type": "artifact", "id": "a003"},
                    ],
                },
            },
        )
    ]


def test_reason_cli_intent_propose_keeps_time_budget_out_of_description(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "rw_000001", "status": "open", "draft": {}}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "reason",
            "intent",
            "propose",
            "--todo-ref",
            "authz",
            "--from-fact",
            "f001",
            "--description",
            "验证订单详情接口是否存在对象级授权问题",
            "--target",
            "a.example.com",
            "--execute-seconds",
            "420",
            "--conclude-seconds",
            "180",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    proposal = calls[0][2]["intent"]
    assert proposal["description"] == "验证订单详情接口是否存在对象级授权问题"
    assert proposal["execute_seconds"] == 420
    assert proposal["conclude_seconds"] == 180
    assert "timeout" not in proposal["description"]


def test_reason_cli_intent_propose_allows_omitting_target(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "rw_000001", "status": "open", "draft": {}}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "reason",
            "intent",
            "propose",
            "--todo-ref",
            "miniapp",
            "--from-fact",
            "f001",
            "--description",
            "验证小程序投诉流程",
            "--execute-seconds",
            "420",
            "--conclude-seconds",
            "180",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["intent"]["targets"] == []


def test_explore_cli_report_does_not_allow_worker_to_choose_identity(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"total": 1, "duplicate": False}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "vulnerability",
            "report",
            "--title",
            "对象级授权缺失",
            "--type",
            "越权",
            "--severity",
            "high",
            "--description",
            "低权限用户读取了其他用户的数据。",
            "--prerequisites",
            "需要任意低权限账号。",
            "--impact",
            "导致数据泄露。",
            "--remediation",
            "服务端校验对象归属。",
            "--evidence-path",
            "evidence/request.txt",
            "--evidence-path",
            "evidence/response.txt",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/dispatch/tasks/t_001/intents/i003/explore-vulnerabilities"
    assert payload["lease_id"] == "lease-1"
    assert "intent_id" not in payload["report"]
    assert "source" not in payload["report"]
    assert "reported_by" not in payload["report"]
    assert "status" not in payload["report"]
    assert payload["report"]["prerequisites"] == "需要任意低权限账号。"
    assert payload["report"]["remediation"] == "服务端校验对象归属。"
    assert payload["report"]["evidence_paths"] == [
        "evidence/request.txt",
        "evidence/response.txt",
    ]
    assert json.loads(result.output)["total"] == 1


def test_explore_cli_marks_human_intervention_with_reason(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {
            "task_id": "t_001",
            "intent_id": "i003",
            "required": True,
            "reason": "需要人工完成短信验证码",
            "marked_at": "2026-08-10T00:00:00Z",
            "updated": True,
        }

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "human-intervention",
            "mark",
            "--reason",
            "需要人工完成短信验证码",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "/dispatch/tasks/t_001/intents/i003/human-intervention",
            {
                "lease_id": "lease-1",
                "reason": "需要人工完成短信验证码",
            },
        )
    ]
    assert json.loads(result.output)["updated"] is True


def test_explore_cli_reads_human_intervention_reason_file(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict]] = []
    reason_file = tmp_path / "reason.md"
    reason_file.write_text("需要人工在真实设备上完成扫码授权\n", encoding="utf-8")

    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)) or {},
    )
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "human-intervention",
            "mark",
            "--reason-file",
            str(reason_file),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["reason"] == "需要人工在真实设备上完成扫码授权"


def test_explore_cli_rejects_two_or_empty_human_intervention_reasons(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)),
    )

    both = CliRunner().invoke(
        main,
        [
            "explore",
            "human-intervention",
            "mark",
            "--reason",
            "需要人工操作",
            "--reason-file",
            "/tmp/reason.md",
        ],
        env=_env(),
    )
    empty = CliRunner().invoke(
        main,
        ["explore", "human-intervention", "mark", "--reason", "  "],
        env=_env(),
    )

    assert both.exit_code == 2
    assert "either --reason or its --*-file option" in both.output
    assert empty.exit_code == 2
    assert "reason is required" in empty.output
    assert calls == []


def test_explore_cli_allows_report_without_an_evidence_path(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"total": 1, "duplicate": False}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "vulnerability",
            "report",
            "--title",
            "对象级授权缺失",
            "--type",
            "越权",
            "--severity",
            "high",
            "--description",
            "描述",
            "--prerequisites",
            "无需额外权限",
            "--impact",
            "影响",
            "--remediation",
            "增加服务端鉴权",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["report"]["evidence_paths"] == []


def test_explore_cli_requires_prerequisites_and_remediation(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)),
    )
    base = [
        "explore",
        "vulnerability",
        "report",
        "--title",
        "对象级授权缺失",
        "--type",
        "越权",
        "--severity",
        "high",
        "--description",
        "描述",
        "--impact",
        "影响",
    ]

    missing_prerequisites = CliRunner().invoke(
        main,
        [*base, "--remediation", "增加服务端鉴权"],
        env=_env(),
    )
    missing_remediation = CliRunner().invoke(
        main,
        [*base, "--prerequisites", "无需额外权限"],
        env=_env(),
    )

    assert missing_prerequisites.exit_code == 2
    assert "prerequisites is required" in missing_prerequisites.output
    assert missing_remediation.exit_code == 2
    assert "remediation is required" in missing_remediation.output
    assert calls == []


def test_explore_cli_reads_required_fields_from_files(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"total": 1, "duplicate": False}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    prerequisites = tmp_path / "prerequisites.txt"
    remediation = tmp_path / "remediation.txt"
    prerequisites.write_text("需要低权限账号\n", encoding="utf-8")
    remediation.write_text("校验对象归属\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "explore",
            "vulnerability",
            "report",
            "--title",
            "对象级授权缺失",
            "--type",
            "越权",
            "--severity",
            "high",
            "--description",
            "描述",
            "--prerequisites-file",
            str(prerequisites),
            "--impact",
            "影响",
            "--remediation-file",
            str(remediation),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["report"]["prerequisites"] == "需要低权限账号"
    assert calls[0][2]["report"]["remediation"] == "校验对象归属"


def test_credential_cli_lists_metadata_through_the_current_intent_lease(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return [{"id": "cred_001", "name": "登录 Cookie"}]

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID")
    result = CliRunner().invoke(main, ["credential", "list"], env=environment)

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "/dispatch/tasks/t_001/credentials/list",
            {"lease_id": "lease-1", "intent_id": "i003"},
        )
    ]
    assert "raw-secret" not in result.output


def test_credential_cli_uses_reason_run_identity_when_reason_is_active(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return []

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment.pop("AXIOM_INTENT_ID")
    result = CliRunner().invoke(main, ["credential", "list"], env=environment)

    assert result.exit_code == 0, result.output
    assert calls[0][2] == {"lease_id": "lease-1", "reason_run_id": "rr000001"}


def test_credential_cli_uses_plan_run_identity_when_plan_is_active(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return []

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment.pop("AXIOM_INTENT_ID")
    environment.pop("AXIOM_REASON_RUN_ID")
    environment["AXIOM_PLAN_RUN_ID"] = "pr000001"
    result = CliRunner().invoke(main, ["credential", "list"], env=environment)

    assert result.exit_code == 0, result.output
    assert calls[0][2] == {"lease_id": "lease-1", "plan_run_id": "pr000001"}


def test_credential_cli_writes_material_privately_without_echoing_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_request(_method: str, _path: str, _payload: dict):
        return {
            "id": "cred_001",
            "name": "登录 Cookie",
            "content": "Cookie: session=raw-secret\nX-CSRF: token",
            "notes": "低权限账号",
            "refresh_id": "crf_001",
        }

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID")
    environment["AXIOM_RUN_DIR"] = str(tmp_path)
    result = CliRunner().invoke(
        main,
        ["credential", "get", "cred_001", "--output", "private/credential.md"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    destination = tmp_path / "private" / "credential.md"
    private_text = destination.read_text(encoding="utf-8")
    assert "名称: 登录 Cookie" in private_text
    assert "凭证刷新 ID: crf_001" in private_text
    assert "低权限账号" in private_text
    assert "Cookie: session=raw-secret\nX-CSRF: token" in private_text
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert "raw-secret" not in result.output
    assert json.loads(result.output) == {
        "id": "cred_001",
        "output": str(destination),
    }


def test_credential_cli_writes_refresh_instructions_without_echoing_them(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, dict]] = []
    refresh_content = "POST /refresh HTTP/1.1\n\nrefresh_token=very-secret"

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "crf_001", "content": refresh_content}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID")
    environment["AXIOM_RUN_DIR"] = str(tmp_path)
    result = CliRunner().invoke(
        main,
        ["credential", "refresh", "crf_001", "--output", "private/refresh.txt"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    destination = tmp_path / "private" / "refresh.txt"
    assert destination.read_text(encoding="utf-8") == refresh_content + "\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert "very-secret" not in result.output
    assert calls == [
        (
            "POST",
            "/dispatch/tasks/t_001/credential-refreshes/crf_001/get",
            {"lease_id": "lease-1", "intent_id": "i003"},
        )
    ]
    assert json.loads(result.output) == {
        "id": "crf_001",
        "output": str(destination),
    }


def test_credential_cli_rejects_output_outside_the_run_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda *_args: {"id": "cred_001", "name": "Cookie", "content": "secret"},
    )
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID")
    environment["AXIOM_RUN_DIR"] = str(tmp_path / "run")

    result = CliRunner().invoke(
        main,
        ["credential", "get", "cred_001", "--output", str(tmp_path / "outside.json")],
        env=environment,
    )

    assert result.exit_code != 0
    assert not (tmp_path / "outside.json").exists()


def test_credential_cli_rejects_symlink_destination(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda *_args: {"id": "cred_001", "name": "Cookie", "content": "secret"},
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    destination = run_dir / "credential.md"
    destination.symlink_to(outside)
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID")
    environment["AXIOM_RUN_DIR"] = str(run_dir)

    result = CliRunner().invoke(
        main,
        ["credential", "get", "cred_001", "--output", "credential.md"],
        env=environment,
    )

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_review_cli_status_and_decision_use_the_active_review_lease(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"ready": True, "errors": []}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    status = CliRunner().invoke(main, ["review", "status"], env=_review_env())
    decision = CliRunner().invoke(
        main,
        [
            "review",
            "decision",
            "submit",
            "duplicate",
            "--duplicate-of",
            "v000",
            "--reason",
            "与更早漏洞属于同一根因和影响对象",
        ],
        env=_review_env(),
    )

    assert status.exit_code == 0, status.output
    assert decision.exit_code == 0, decision.output
    assert calls == [
        (
            "POST",
            "/dispatch/tasks/t_001/vulnerability-review-runs/vr000001/status",
            {"lease_id": "lease-1"},
        ),
        (
            "POST",
            "/dispatch/tasks/t_001/vulnerability-review-runs/vr000001/decision",
            {
                "lease_id": "lease-1",
                "decision": "duplicate",
                "reason": "与更早漏洞属于同一根因和影响对象",
                "duplicate_of": "v000",
            },
        ),
    ]


def test_review_cli_rejects_invalid_duplicate_options_before_request(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)),
    )

    missing_target = CliRunner().invoke(
        main,
        [
            "review",
            "decision",
            "submit",
            "duplicate",
            "--reason",
            "属于重复漏洞但缺少目标",
        ],
        env=_review_env(),
    )
    unexpected_target = CliRunner().invoke(
        main,
        [
            "review",
            "decision",
            "submit",
            "verified",
            "--duplicate-of",
            "v001",
            "--reason",
            "证据已经充分证明漏洞成立",
        ],
        env=_review_env(),
    )

    assert missing_target.exit_code == 2
    assert "duplicate requires --duplicate-of" in missing_target.output
    assert unexpected_target.exit_code == 2
    assert "--duplicate-of is only valid for duplicate" in unexpected_target.output
    assert calls == []


def test_credential_cli_supports_review_run_identity(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return []

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(main, ["credential", "list"], env=_review_env())

    assert result.exit_code == 0, result.output
    assert calls[0][2] == {
        "lease_id": "lease-1",
        "review_run_id": "vr000001",
    }


def _plan_env() -> dict[str, str]:
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID", None)
    environment.pop("AXIOM_REASON_WORKSPACE_ID", None)
    environment["AXIOM_PLAN_RUN_ID"] = "pr000001"
    environment["AXIOM_PLAN_WORKSPACE_ID"] = "pw_000001"
    return environment


def test_reason_cli_stages_chain_with_objective_bounded_targets(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "rw_000001", "status": "open", "draft": {}}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        [
            "reason",
            "chain",
            "add",
            "--ref",
            "ch-1",
            "--title",
            "从普通成员到租户管理员",
            "--capability",
            "tenant:admin",
            "--asset",
            "member-management",
            "--from-fact",
            "f001",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "/reason-workspaces/rw_000001/chains",
            {
                "lease_id": "lease-1",
                "chain": {
                    "ref": "ch-1",
                    "title": "从普通成员到租户管理员",
                    "target_capabilities": ["tenant:admin"],
                    "target_assets": ["member-management"],
                    "source_fact_ids": ["f001"],
                },
            },
        )
    ]


def test_reason_cli_chain_link_requires_exactly_one_chain_reference(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    both = CliRunner().invoke(
        main,
        [
            "reason",
            "chain",
            "link",
            "--chain-ref",
            "ch-1",
            "--chain-id",
            "ch000001",
            "--intent-ref",
            "i1",
        ],
        env=_env(),
    )
    neither = CliRunner().invoke(
        main,
        ["reason", "chain", "link", "--intent-ref", "i1"],
        env=_env(),
    )

    assert both.exit_code == 2
    assert "exactly one of --chain-ref or --chain-id is required" in both.output
    assert neither.exit_code == 2
    assert calls == []


def test_plan_cli_stages_chain_link_against_workspace_draft(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "pw_000001", "status": "open", "draft": {}}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    result = CliRunner().invoke(
        main,
        ["plan", "chain", "link", "--chain-ref", "ch-1", "--intent-ref", "i1"],
        env=_plan_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "POST",
            "/plan-workspaces/pw_000001/chain-links",
            {
                "lease_id": "lease-1",
                "link": {"chain_ref": "ch-1", "intent_ref": "i1"},
            },
        )
    ]


def test_explore_cli_result_report_defaults_to_the_bound_chain(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"id": "rr000001", "status": "reviewing"}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment["AXIOM_CHAIN_ID"] = "ch000123"
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "result",
            "report",
            "--type",
            "permission",
            "--title",
            "取得成员管理能力",
            "--description",
            "复用普通成员会话调用成员管理接口成功。",
            "--principal",
            "user:member",
            "--capability-gained",
            "tenant:admin",
            "--target-asset",
            "member-management",
            "--evidence-path",
            "evidence/result.txt",
        ],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/tasks/t_001/intents/i003/red-team-results"
    assert payload["report"]["chain_id"] == "ch000123"
    assert payload["report"]["capabilities_gained"] == ["tenant:admin"]

    without_binding = CliRunner().invoke(
        main,
        [
            "explore",
            "result",
            "report",
            "--type",
            "permission",
            "--title",
            "取得成员管理能力",
            "--description",
            "复用普通成员会话调用成员管理接口成功。",
            "--principal",
            "user:member",
            "--evidence-path",
            "evidence/result.txt",
        ],
        env=_env(),
    )
    assert without_binding.exit_code == 2
    assert "--chain-id is required" in without_binding.output
