from __future__ import annotations

import json
import stat
from pathlib import Path

from click.testing import CliRunner

from axiom.cli import main


# 上报漏洞/成果时必须带结构化复现。报文逐字真实，不是 curl。
REPRODUCTION = {
    "entry_point": "GET /api/users/2",
    "prerequisites": ["持有普通用户会话"],
    "steps": [
        {
            "title": "用普通用户会话读取他人资料",
            "request": (
                "GET /api/users/2 HTTP/1.1\r\n"
                "Host: a.example.com\r\n"
                "Cookie: sid=member\r\n"
                "\r\n"
            ),
            "response": 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"id":2,"phone":"138"}',
            "proves": "普通用户可读取他人资料，对象级授权缺失",
        }
    ],
}


def _reproduction_file(tmp_path: Path) -> Path:
    path = tmp_path / "reproduction.json"
    path.write_text(json.dumps(REPRODUCTION, ensure_ascii=False), encoding="utf-8")
    return path

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


def test_explore_cli_report_does_not_allow_worker_to_choose_identity(monkeypatch, tmp_path: Path) -> None:
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
            "--reproduction-file",
            str(_reproduction_file(tmp_path)),
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
    assert payload["report"]["reproduction"]["entry_point"] == "GET /api/users/2"
    assert payload["report"]["reproduction"]["steps"][0]["proves"].startswith("普通用户可读取")
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


def test_explore_cli_requires_a_reproduction_file(monkeypatch, tmp_path: Path) -> None:
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

    # 复现是上报的必需项：没有可复现路径就不是已确认漏洞。
    assert result.exit_code == 2
    assert "--reproduction-file" in result.output
    assert calls == []


def test_explore_cli_requires_prerequisites_but_not_remediation(monkeypatch, tmp_path: Path) -> None:
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

    reproduction = str(_reproduction_file(tmp_path))
    missing_prerequisites = CliRunner().invoke(
        main,
        [*base, "--remediation", "增加服务端鉴权", "--reproduction-file", reproduction],
        env=_env(),
    )
    # 修复建议不再是 Explore 的必填项：交付报告里的这一节由审核/AI 富化补。
    without_remediation = CliRunner().invoke(
        main,
        [*base, "--prerequisites", "无需额外权限", "--reproduction-file", reproduction],
        env=_env(),
    )

    # 复现里已有 prerequisites，所以 --prerequisites 可以省略：它只是那句概括，
    # 报告展示的是复现里那一条条条件，两边都写等于同一件事说两遍。
    assert missing_prerequisites.exit_code == 0, missing_prerequisites.output
    assert without_remediation.exit_code == 0, without_remediation.output
    # 第一次调用带了 --remediation，第二次没带；后者应回落到空串
    assert calls[0][2]["report"]["remediation"] == "增加服务端鉴权"
    assert calls[-1][2]["report"]["remediation"] == ""
    assert calls[0][2]["report"]["prerequisites"] == ""
    assert calls[0][2]["report"]["reproduction"]["prerequisites"] == REPRODUCTION["prerequisites"]


def test_explore_cli_still_requires_prerequisites_when_the_reproduction_has_none(
    monkeypatch, tmp_path: Path
) -> None:
    """两边都没有 = 没说清怎么利用。这时 --prerequisites 必须给。"""
    monkeypatch.setattr(
        "axiom.worker_cli._request", lambda method, path, payload: {"total": 1}
    )
    bare = dict(REPRODUCTION, prerequisites=[])
    path = tmp_path / "bare.json"
    path.write_text(json.dumps(bare, ensure_ascii=False), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "explore", "vulnerability", "report",
            "--title", "对象级授权缺失", "--type", "越权", "--severity", "high",
            "--description", "描述", "--impact", "影响",
            "--reproduction-file", str(path),
        ],
        env=_env(),
    )

    assert result.exit_code == 2
    assert "prerequisites is required" in result.output


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
            "--reproduction-file",
            str(_reproduction_file(tmp_path)),
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


def test_review_cli_description_submit_posts_the_description_to_the_review_run(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"applied_fields": ["description", "reproduction_json"], "ready": False}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    reproduction = _reproduction_file(tmp_path)
    description = tmp_path / "description.md"
    description.write_text("审核者改写后的描述。", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "review",
            "description",
            "submit",
            "--description-file",
            str(description),
            "--reproduction-file",
            str(reproduction),
        ],
        env=_review_env(),
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/dispatch/tasks/t_001/vulnerability-review-runs/vr000001/description"
    assert payload["lease_id"] == "lease-1"
    assert payload["description"] == "审核者改写后的描述。"
    # 复现在本地就被解析并规范化：旧的 request/response 形状会折成 exchanges 数组
    assert payload["reproduction"]["entry_point"] == "GET /api/users/2"
    assert len(payload["reproduction"]["steps"]) == 1
    assert len(payload["reproduction"]["steps"][0]["exchanges"]) == 1
    assert payload["reproduction"]["steps"][0]["exchanges"][0]["request"].startswith(
        "GET /api/users/2 HTTP/1.1"
    )


def test_review_cli_description_submit_allows_description_without_reproduction(
    monkeypatch,
    tmp_path,
) -> None:
    """只想修描述时不该被迫把整份复现重交一遍。"""
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return {"applied_fields": ["description"], "ready": True}

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    description = tmp_path / "description.md"
    description.write_text("只改描述。", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "review",
            "description",
            "submit",
            "--description-file",
            str(description),
        ],
        env=_review_env(),
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][2] == {"lease_id": "lease-1", "description": "只改描述。"}


def test_review_cli_description_submit_rejects_a_malformed_reproduction_file(
    monkeypatch,
    tmp_path,
) -> None:
    """坏报文要在本地就报错：审核者拿到的是可读提示，不是一个裸 422。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)),
    )
    broken = tmp_path / "reproduction.json"
    broken.write_text(json.dumps({"steps": []}), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "review",
            "description",
            "submit",
            "--description",
            "改写后的描述",
            "--reproduction-file",
            str(broken),
        ],
        env=_review_env(),
    )

    assert result.exit_code != 0
    assert calls == []


def test_review_cli_description_submit_rejects_two_sources_for_one_field(
    monkeypatch,
    tmp_path,
) -> None:
    """同一字段同时给 --x 和 --x-file 是用法错误，不该发请求。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)),
    )
    reproduction = _reproduction_file(tmp_path)
    description = tmp_path / "description.md"
    description.write_text("改写后的描述", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "review",
            "description",
            "submit",
            "--description",
            "改写后的描述",
            "--description-file",
            str(description),
            "--reproduction-file",
            str(reproduction),
        ],
        env=_review_env(),
    )

    assert result.exit_code == 2
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


def test_explore_cli_result_report_defaults_to_the_bound_chain(monkeypatch, tmp_path: Path) -> None:
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
            "--points",
            "30",
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
            "--reproduction-file",
            str(_reproduction_file(tmp_path)),
        ],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    method, path, payload = calls[0]
    assert method == "POST"
    assert path == "/tasks/t_001/intents/i003/red-team-results"
    assert "chain_id" not in payload["report"]
    assert payload["report"]["points_declared"] == 30
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
            "--reproduction-file",
            str(_reproduction_file(tmp_path)),
        ],
        env=_env(),
    )
    assert without_binding.exit_code == 2
    assert "Missing option" in without_binding.output


def test_result_report_no_longer_takes_evidence_files(monkeypatch) -> None:
    """Evidence files are gone from the Result path: the reproduction is the proof.

    A worker that still passes --evidence-path gets a usage error naming the option
    rather than a silent no-op.
    """

    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)) or {},
    )
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "result",
            "report",
            "--points",
            "20",
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

    assert result.exit_code == 2
    assert "No such option" in result.output
    assert calls == []


def test_result_report_reports_the_parsed_reproduction(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        "axiom.worker_cli._request",
        lambda method, path, payload: calls.append((method, path, payload)) or {"id": "x001"},
    )
    result = CliRunner().invoke(
        main,
        [
            "explore",
            "result",
            "report",
            "--points",
            "20",
            "--type",
            "permission",
            "--title",
            "取得成员管理能力",
            "--description",
            "复用普通成员会话调用成员管理接口成功。",
            "--principal",
            "user:member",
            "--reproduction-file",
            str(_reproduction_file(tmp_path)),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["report"]["reproduction"]["steps"][0]["title"].startswith("用普通用户会话")
    assert "evidence_refs" not in calls[0][2]["report"]
