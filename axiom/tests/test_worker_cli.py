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
            "90",
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    proposal = calls[0][2]["intent"]
    assert proposal["description"] == "验证订单详情接口是否存在对象级授权问题"
    assert proposal["execute_seconds"] == 420
    assert proposal["conclude_seconds"] == 90
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
    assert json.loads(result.output)["total"] == 1


def test_credential_cli_lists_metadata_through_the_current_intent_lease(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method: str, path: str, payload: dict):
        calls.append((method, path, payload))
        return [{"id": "cred_001", "name": "登录 Cookie", "data_fields": ["Cookie"]}]

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
            "credential_type": "headers",
            "target_host": "a.example.com",
            "data": {"Cookie": "session=raw-secret"},
            "enabled": True,
            "project_id": "p001",
            "created_at": "2026-07-27T00:00:00Z",
            "updated_at": "2026-07-27T00:00:00Z",
        }

    monkeypatch.setattr("axiom.worker_cli._request", fake_request)
    environment = _env()
    environment.pop("AXIOM_REASON_RUN_ID")
    environment["AXIOM_RUN_DIR"] = str(tmp_path)
    result = CliRunner().invoke(
        main,
        ["credential", "get", "cred_001", "--output", "private/credential.json"],
        env=environment,
    )

    assert result.exit_code == 0, result.output
    destination = tmp_path / "private" / "credential.json"
    assert json.loads(destination.read_text(encoding="utf-8"))["data"]["Cookie"] == "session=raw-secret"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert "raw-secret" not in result.output


def test_credential_cli_rejects_output_outside_the_run_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("axiom.worker_cli._request", lambda *_args: {"data": {"Cookie": "secret"}})
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
