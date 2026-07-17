from __future__ import annotations

import pytest

from axiom.dispatcher.tools.gateway import ToolGateway
from axiom.dispatcher.task_scope import RuntimeTaskScope
import axiom.dispatcher.tools.gateway as gateway_module


def test_gateway_classifies_builtin_tool() -> None:
    gateway = ToolGateway()
    invocation = gateway.prepare("explore_execute", ["curl", "-sS", "https://example.com"])
    assert invocation.tool == "curl"
    assert invocation.kind == "builtin"
    assert invocation.argv == ["curl", "-sS", "https://example.com"]


def test_gateway_classifies_external_tool() -> None:
    gateway = ToolGateway()
    invocation = gateway.prepare("explore_execute", ["/opt/custom/bin/mytool", "--json"])
    assert invocation.tool == "mytool"
    assert invocation.kind == "external"


def test_gateway_blocks_configured_tools() -> None:
    gateway = ToolGateway(blocked_tools={"masscan"})
    with pytest.raises(ValueError, match="tool blocked by policy"):
        gateway.prepare("explore_execute", ["masscan", "1.1.1.1"])


def test_gateway_blocks_blacklisted_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = RuntimeTaskScope(True, frozenset({"*.kkroup.cn"}), frozenset({"git.kkroup.cn"}))
    monkeypatch.setattr(gateway_module, "load_task_scope", lambda _project_id: scope)
    gateway = ToolGateway()
    with pytest.raises(ValueError, match="domain outside task scope"):
        gateway.prepare("explore_execute", ["curl", "https://git.kkroup.cn/v1"], project_id="proj_001")


def test_gateway_does_not_block_non_explore_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = RuntimeTaskScope(True, frozenset({"example.com"}), frozenset({"git.kkroup.cn"}))
    monkeypatch.setattr(gateway_module, "load_task_scope", lambda _project_id: scope)
    gateway = ToolGateway()
    invocation = gateway.prepare("reason_execute", ["curl", "https://git.kkroup.cn"], project_id="proj_001")
    assert invocation.tool == "curl"


def test_gateway_blocks_host_outside_task_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = RuntimeTaskScope(True, frozenset({"*.example.com"}), frozenset())
    monkeypatch.setattr(gateway_module, "load_task_scope", lambda _project_id: scope)
    gateway = ToolGateway()
    with pytest.raises(ValueError, match="domain outside task scope"):
        gateway.prepare("bootstrap_execute", ["curl", "https://outside.test"], project_id="proj_001")


def test_gateway_does_not_treat_pi_model_filename_as_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = RuntimeTaskScope(True, frozenset({"127.0.0.1"}), frozenset())
    monkeypatch.setattr(gateway_module, "load_task_scope", lambda _project_id: scope)
    gateway = ToolGateway()

    invocation = gateway.prepare(
        "bootstrap_execute",
        [
            "/opt/pi/bin/pi",
            "--model",
            "models.json",
            "--prompt",
            "Inspect http://127.0.0.1:8888 and report the attack surface.",
        ],
        project_id="proj_001",
    )

    assert invocation.tool == "pi"
