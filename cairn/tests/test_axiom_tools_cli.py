from __future__ import annotations

import json

from click.testing import CliRunner

from axiom.tools_cli import main


class FakeResponse:
    ok = True
    status_code = 200
    content = b"{}"

    def json(self):
        return {"ok": True}


def test_axiom_tool_reason_commands_use_workspace_environment(monkeypatch) -> None:
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("axiom.tools_cli.requests.request", fake_request)
    runner = CliRunner()
    env = {
        "AXIOM_SERVER": "http://axiom.test",
        "AXIOM_REASON_WORKSPACE": "rw_000001",
        "AXIOM_SERVICE_TOKEN": "service-token",
    }

    result = runner.invoke(
        main,
        [
            "todo",
            "add",
            "--ref",
            "t1",
            "--title",
            "Verify JWT",
            "--category",
            "authentication",
            "--source",
            "origin:origin",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == (
        "POST",
        "http://axiom.test/api/v1/reason-workspaces/rw_000001/todos",
    )
    assert calls[0][2]["json"]["sources"] == [{"type": "origin", "id": "origin"}]
    assert calls[0][2]["headers"]["Authorization"] == "Bearer service-token"

    result = runner.invoke(main, ["reason", "commit"], env=env)
    assert result.exit_code == 0, result.output
    assert calls[1][1].endswith("/api/v1/reason-workspaces/rw_000001/commit")


def test_axiom_tool_requires_workspace_environment() -> None:
    result = CliRunner().invoke(main, ["reason", "commit"], env={})
    assert result.exit_code != 0
    assert "AXIOM_REASON_WORKSPACE" in result.output


def test_axiom_tool_context_commands_batch_entity_expansion(monkeypatch) -> None:
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("axiom.tools_cli.requests.request", fake_request)
    env = {
        "AXIOM_SERVER": "http://axiom.test",
        "AXIOM_TASK_ID": "t_001",
        "AXIOM_SERVICE_TOKEN": "service-token",
    }
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["context", "get", "--fact", "f001", "--fact", "f002", "--intent", "i003"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert calls[0][0:2] == (
        "POST",
        "http://axiom.test/api/v1/internal/tasks/t_001/context/expand",
    )
    assert calls[0][2]["json"]["facts"] == ["f001", "f002"]
    assert calls[0][2]["json"]["intents"] == ["i003"]

    result = runner.invoke(main, ["context", "related", "--intent", "i003"], env=env)
    assert result.exit_code == 0, result.output
    assert calls[1][0:2] == (
        "GET",
        "http://axiom.test/api/v1/internal/tasks/t_001/context/intents/i003/related",
    )


def test_credential_get_writes_private_file_without_stdout_secret(monkeypatch, tmp_path) -> None:
    class CredentialResponse(FakeResponse):
        def json(self):
            return {
                "id": "cred_001",
                "label": "low-priv",
                "domains": ["api.example.com"],
                "data": {"access_token": "secret-token-value"},
            }

    monkeypatch.setattr(
        "axiom.tools_cli.requests.request",
        lambda *_args, **_kwargs: CredentialResponse(),
    )
    output = tmp_path / "credential-cred_001.json"
    result = CliRunner().invoke(
        main,
        ["credential", "get", "cred_001", "--output", str(output)],
        env={
            "AXIOM_SERVER": "http://axiom.test",
            "AXIOM_SERVICE_TOKEN": "service-token",
            "AXIOM_RUN_DIR": str(tmp_path),
        },
    )
    assert result.exit_code == 0, result.output
    assert "secret-token-value" not in result.output
    assert "access_token" in result.output
    assert json.loads(output.read_text(encoding="utf-8"))["data"]["access_token"] == "secret-token-value"
    assert output.stat().st_mode & 0o777 == 0o600


def test_credential_get_rejects_output_outside_run_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("axiom.tools_cli.requests.request", lambda *_a, **_k: FakeResponse())
    result = CliRunner().invoke(
        main,
        ["credential", "get", "cred_001", "--output", str(tmp_path.parent / "credential-leak.json")],
        env={"AXIOM_RUN_DIR": str(tmp_path)},
    )
    assert result.exit_code != 0
    assert "inside AXIOM_RUN_DIR" in result.output


def test_vulnerability_report_injects_worker_context(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("axiom.tools_cli.requests.request", fake_request)
    report = tmp_path / "vulnerability.json"
    report.write_text(
        json.dumps(
            {
                "title": "越权读取其他用户资料",
                "type": "越权访问",
                "severity": "high",
                "description": "低权限用户读取了其他用户资料。",
                "impact": "泄露其他用户的真实资料。",
                "reproduction_steps": ["使用用户 A 请求用户 B 的 ID"],
                "evidence": ["findings.md#越权读取"],
                "affected_target": "api.example.com",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        ["vulnerability", "report", "--file", str(report)],
        env={
            "AXIOM_SERVER": "http://axiom.test",
            "AXIOM_SERVICE_TOKEN": "service-token",
            "AXIOM_TASK_ID": "t_001",
            "AXIOM_INTENT_ID": "i007",
            "AXIOM_WORKER": "worker-a",
            "AXIOM_PHASE": "explore",
        },
    )
    assert result.exit_code == 0, result.output
    payload = calls[0][2]["json"]
    assert payload["intent_id"] == "i007"
    assert payload["reported_by"] == "worker-a"
    assert payload["source"] == "explore"


def test_vulnerability_report_rejects_incomplete_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("axiom.tools_cli.requests.request", lambda *_a, **_k: FakeResponse())
    report = tmp_path / "vulnerability.json"
    report.write_text(
        json.dumps(
            {
                "title": "理论风险",
                "type": "configuration",
                "severity": "medium",
                "description": "仅观察到配置。",
                "impact": "尚未证明。",
                "affected_target": "api.example.com",
                "evidence": [],
                "reproduction_steps": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        ["vulnerability", "report", "--file", str(report)],
        env={
            "AXIOM_TASK_ID": "t_001",
            "AXIOM_INTENT_ID": "i002",
            "AXIOM_WORKER": "worker-a",
        },
    )
    assert result.exit_code != 0
    assert "evidence" in result.output
    assert "reproduction_steps" in result.output
