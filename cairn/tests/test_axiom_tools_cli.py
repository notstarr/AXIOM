from __future__ import annotations

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
