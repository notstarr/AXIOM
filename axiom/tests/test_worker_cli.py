from __future__ import annotations

import json

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
