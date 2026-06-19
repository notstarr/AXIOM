from __future__ import annotations

import pytest

from axiom.dispatcher.tools.gateway import ToolGateway
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
    monkeypatch.setattr(gateway_module, "load_domain_blacklist", lambda: {"git.kkroup.cn"})
    gateway = ToolGateway()
    with pytest.raises(ValueError, match="blocked domain by policy"):
        gateway.prepare("explore_execute", ["curl", "https://api.git.kkroup.cn/v1"])


def test_gateway_does_not_block_non_explore_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_module, "load_domain_blacklist", lambda: {"git.kkroup.cn"})
    gateway = ToolGateway()
    invocation = gateway.prepare("reason_execute", ["curl", "https://git.kkroup.cn"])
    assert invocation.tool == "curl"
