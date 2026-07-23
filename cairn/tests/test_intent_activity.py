from __future__ import annotations

from axiom.dispatcher.runtime.activity import summarize_tool_activity


def test_bash_activity_uses_high_level_summary_without_exposing_command() -> None:
    command = "HTTP_PROXY=http://user:secret@proxy nmap -sV 10.0.0.8 --script vuln"
    summary = summarize_tool_activity("bash", {"command": command}, ["10.0.0.8"])

    assert summary == "正在扫描 10.0.0.8 的开放端口和服务"
    assert "secret" not in summary
    assert "--script" not in summary


def test_native_and_unknown_tools_have_readable_fallbacks() -> None:
    assert summarize_tool_activity("read", {"path": "/tmp/context.json"}, []) == "正在读取任务资料或测试产物"
    assert summarize_tool_activity("mcp__custom__probe", {"token": "secret"}, ["example.com"]) == "正在使用 mcp__custom__probe 执行测试"
