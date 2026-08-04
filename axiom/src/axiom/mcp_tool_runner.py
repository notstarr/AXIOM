from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Invoke one snapshotted AXIOM MCP Tool")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--tool-id", required=True)
    return parser.parse_args()


def _load(path: str, tool_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 MCP 能力快照：{snapshot_path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MCP 能力快照格式错误")
    tools = payload.get("mcp_tools")
    servers = payload.get("mcp_servers")
    if not isinstance(tools, list) or not isinstance(servers, list):
        raise RuntimeError("MCP 能力快照缺少工具或 Server 配置")
    tool = next(
        (
            item
            for item in tools
            if isinstance(item, dict) and str(item.get("key") or "") == tool_id
        ),
        None,
    )
    if tool is None:
        raise RuntimeError(f"MCP Tool 不在本 Lease 能力快照中：{tool_id}")
    server_id = str(tool.get("server_id") or "")
    server = next(
        (
            item
            for item in servers
            if isinstance(item, dict) and str(item.get("id") or "") == server_id
        ),
        None,
    )
    if server is None or not isinstance(server.get("config"), dict):
        raise RuntimeError(f"MCP Server 配置不在本 Lease 能力快照中：{server_id}")
    return tool, server


@asynccontextmanager
async def _transport(
    server: dict[str, Any],
) -> AsyncIterator[tuple[Any, Any]]:
    config = server["config"]
    command = str(config.get("command") or "")
    if command:
        environment = {
            **os.environ,
            **{
                str(key): str(value)
                for key, value in (config.get("env") or {}).items()
            },
        }
        parameters = StdioServerParameters(
            command=command,
            args=[str(item) for item in config.get("args") or []],
            env=environment,
        )
        async with stdio_client(parameters) as streams:
            yield streams
        return

    url = str(config.get("url") or "")
    if not url:
        raise RuntimeError("MCP Server 缺少 command 或 url")
    headers = {
        str(key): str(value) for key, value in (config.get("headers") or {}).items()
    }
    transport = str(config.get("transport") or server.get("transport") or "http")
    if transport == "sse":
        async with sse_client(url, headers=headers or None) as streams:
            yield streams
        return
    async with streamablehttp_client(url, headers=headers or None) as streams:
        yield streams[:2]


async def _invoke(
    tool: dict[str, Any],
    server: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    async with _transport(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(
                str(tool.get("tool_name") or ""),
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=120),
            )
    dumped = result.model_dump(mode="json", exclude_none=True)
    return {
        "server": tool.get("server_name"),
        "tool": tool.get("tool_name"),
        "is_error": bool(result.isError),
        "result": dumped,
    }


def _safe_error(error: Exception, server: dict[str, Any] | None) -> str:
    message = str(error).strip() or error.__class__.__name__
    if server and isinstance(server.get("config"), dict):
        config = server["config"]
        secrets = [
            str(value)
            for value in (config.get("env") or {}).values()
            if len(str(value)) >= 6
        ]
        secrets.extend(
            str(value)
            for value in (config.get("headers") or {}).values()
            if len(str(value)) >= 6
        )
        for secret in secrets:
            message = message.replace(secret, "***")
    return message[-4_000:]


def main() -> int:
    args = _arguments()
    server: dict[str, Any] | None = None
    try:
        tool, server = _load(args.snapshot, args.tool_id)
        raw = sys.stdin.read(1_000_001)
        if len(raw) > 1_000_000:
            raise RuntimeError("MCP Tool 参数超过 1MB 限制")
        arguments = json.loads(raw or "{}")
        if not isinstance(arguments, dict):
            raise RuntimeError("MCP Tool 参数必须是 JSON object")
        output = anyio.run(_invoke, tool, server, arguments)
        sys.stdout.write(json.dumps(output, ensure_ascii=False, default=str))
        return 1 if output["is_error"] else 0
    except Exception as exc:  # noqa: BLE001 - process boundary must serialize failures
        sys.stderr.write(_safe_error(exc, server) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
