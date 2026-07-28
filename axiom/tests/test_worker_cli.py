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
            "--category",
            "authorization",
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
                    "category": "authorization",
                    "theme": "",
                    "priority": 50,
                    "status": "pending",
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
            "--impact",
            "导致数据泄露。",
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
    assert payload["report"]["evidence_paths"] == [
        "evidence/request.txt",
        "evidence/response.txt",
    ]
    assert json.loads(result.output)["total"] == 1


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
            "--impact",
            "影响",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["report"]["evidence_paths"] == []


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
