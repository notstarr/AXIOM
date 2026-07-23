from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


@dataclass(slots=True)
class MCPDiscovery:
    tools: list[dict[str, Any]]
    server_info: dict[str, Any]
    instructions: str = ""


async def discover_mcp_server(server: dict[str, Any], *, timeout_seconds: float = 45) -> MCPDiscovery:
    """Initialize an MCP server and return every page from tools/list."""
    with anyio.fail_after(timeout_seconds):
        if server.get("command"):
            return await _discover_stdio(server)
        if server.get("url"):
            return await _discover_remote(server)
    raise ValueError("MCP Server 必须提供 command 或 URL")


async def _discover_stdio(server: dict[str, Any]) -> MCPDiscovery:
    env = {str(key): str(value) for key, value in (server.get("env") or {}).items()}
    params = StdioServerParameters(
        command=str(server["command"]),
        args=[str(item) for item in (server.get("args") or [])],
        env=env,
    )
    # The SDK requires a real file descriptor for subprocess stderr.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        try:
            async with stdio_client(params, errlog=errlog) as streams:
                return await _read_tools(streams)
        except Exception as exc:
            errlog.seek(0)
            detail = errlog.read()[-2000:].strip()
            if detail:
                raise RuntimeError(f"{_error_detail(exc)}; Server stderr: {detail}") from exc
            raise


async def _discover_remote(server: dict[str, Any]) -> MCPDiscovery:
    url = str(server["url"])
    headers = _headers(server)
    transport = str(server.get("transport") or "http").lower()
    prefer_sse = transport == "sse" or url.rstrip("/").endswith("/sse")
    attempts = ("sse", "http") if prefer_sse else ("http", "sse")
    errors: list[str] = []
    for kind in attempts:
        try:
            if kind == "sse":
                async with sse_client(url, headers=headers, timeout=10, sse_read_timeout=30) as streams:
                    return await _read_tools(streams)
            async with streamablehttp_client(url, headers=headers, timeout=10, sse_read_timeout=30) as streams:
                return await _read_tools(streams[:2])
        except Exception as exc:
            errors.append(f"{kind.upper()}: {_error_detail(exc)}")
    raise RuntimeError("；".join(errors))


async def _read_tools(streams: tuple[Any, Any]) -> MCPDiscovery:
    read_stream, write_stream = streams
    async with ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=30)) as session:
        initialized = await session.initialize()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                payload = tool.model_dump(by_alias=True, exclude_none=True, mode="json")
                tools.append({
                    "name": payload["name"],
                    "title": payload.get("title", ""),
                    "description": payload.get("description", ""),
                    "input_schema": payload.get("inputSchema") or {},
                    "output_schema": payload.get("outputSchema") or {},
                    "annotations": payload.get("annotations") or {},
                    "source": "discovered",
                    "enabled": True,
                })
            cursor = page.nextCursor
            if not cursor:
                break
        info = initialized.serverInfo.model_dump(exclude_none=True, mode="json")
        info["protocol_version"] = initialized.protocolVersion
        return MCPDiscovery(tools=tools, server_info=info, instructions=initialized.instructions or "")


def _headers(server: dict[str, Any]) -> dict[str, str]:
    raw = server.get("raw_config") if isinstance(server.get("raw_config"), dict) else {}
    request_init = raw.get("requestInit") if isinstance(raw.get("requestInit"), dict) else {}
    candidates = raw.get("headers") or raw.get("http_headers") or request_init.get("headers") or {}
    env = {**os.environ, **{str(k): str(v) for k, v in (server.get("env") or {}).items()}}
    result: dict[str, str] = {}
    if isinstance(candidates, dict):
        for key, value in candidates.items():
            text = str(value)
            text = re.sub(r"\$\{([^}]+)\}", lambda match: env.get(match.group(1), match.group(0)), text)
            result[str(key)] = text
    return result


def _error_detail(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        messages = [_error_detail(item) for item in exc.exceptions]
        return " | ".join(dict.fromkeys(message for message in messages if message))
    return str(exc).strip() or exc.__class__.__name__
