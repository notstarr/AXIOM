from __future__ import annotations

import json
import re
import tomllib
import uuid
from collections.abc import Iterable
from typing import Any

import yaml
from sqlalchemy import Engine, delete, select

from axiom.modules.mcp.discovery import discover_mcp_server
from axiom.modules.mcp.schemas import (
    MCPServer,
    MCPServerInput,
    MCPTool,
    MCPWorkerBinding,
    MCPWorkerBindingView,
)
from axiom.modules.mcp.tables import mcp_servers, mcp_tools
from axiom.modules.phase_capabilities.service import PhaseCapabilityService
from axiom.modules.workers.service import WorkerService
from axiom.shared.clock import utcnow
from axiom.shared.errors import ConflictError, NotFoundError


def parse_mcp_config(content: str, format: str = "auto") -> tuple[str, list[MCPServerInput]]:
    raw = content.strip()
    if not raw:
        raise ConflictError("MCP configuration must not be empty")
    selected = format.lower().strip()
    if selected == "auto":
        if raw.startswith(("{", "[")) and "[mcp_servers" not in raw:
            selected = "json"
        elif "[mcp_servers" in raw or "[mcpServers" in raw:
            selected = "toml"
        else:
            selected = "yaml"
    try:
        if selected == "json":
            payload = json.loads(raw)
        elif selected == "toml":
            payload = tomllib.loads(raw)
        elif selected == "yaml":
            payload = yaml.safe_load(raw)
        else:
            raise ConflictError(f"Unsupported MCP format: {selected}")
    except (ValueError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        raise ConflictError(f"Failed to parse MCP {selected}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConflictError("MCP configuration must be an object")

    wrapper = payload.get("mcpServers", payload.get("mcp_servers"))
    if isinstance(wrapper, dict):
        entries = list(wrapper.items())
    elif isinstance(payload.get("servers"), dict):
        entries = list(payload["servers"].items())
    elif any(key in payload for key in ("command", "url", "endpoint", "transport")):
        entries = [(str(payload.get("name") or "server"), payload)]
    else:
        entries = [(name, value) for name, value in payload.items() if isinstance(value, dict)]
    if not entries:
        raise ConflictError("No MCP servers found in configuration")

    result: list[MCPServerInput] = []
    for default_name, value in entries:
        if not isinstance(value, dict):
            raise ConflictError(f"MCP server {default_name} must be an object")
        config = dict(value)
        name = str(config.pop("name", default_name)).strip()
        command = str(config.pop("command", "") or "").strip()
        url = str(config.pop("url", config.pop("endpoint", "")) or "").strip()
        transport = str(config.pop("transport", "") or "").strip().lower()
        args = config.pop("args", []) or []
        env = config.pop("env", {}) or {}
        if not isinstance(args, list):
            raise ConflictError(f"MCP server {name} args must be an array")
        if not isinstance(env, dict):
            raise ConflictError(f"MCP server {name} env must be an object")
        if not transport:
            transport = "stdio" if command else "sse" if url.endswith("/sse") else "http"
        result.append(
            MCPServerInput(
                name=name,
                command=command,
                args=args,
                env=env,
                url=url,
                transport=transport,
                source_format=selected,
                raw_config={
                    **config,
                    "command": command,
                    "args": args,
                    "env": env,
                    "url": url,
                },
            )
        )
    return selected, result


class MCPService:
    def __init__(
        self,
        engine: Engine,
        *,
        phase_capabilities: PhaseCapabilityService | None = None,
    ):
        self.engine = engine
        self.phase_capabilities = phase_capabilities

    def list(self) -> list[MCPServer]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(mcp_servers).order_by(mcp_servers.c.name)).mappings().all()
            tools = self._tools(conn, [row["id"] for row in rows])
            return [self._server(row, tools.get(row["id"], [])) for row in rows]

    def get(self, server_id: str) -> MCPServer:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(mcp_servers).where(mcp_servers.c.id == server_id)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError(f"MCP server not found: {server_id}")
            return self._server(row, self._tools(conn, [server_id]).get(server_id, []))

    def create(self, body: MCPServerInput) -> MCPServer:
        now = utcnow()
        server_id = f"mcp_{uuid.uuid4().hex[:12]}"
        with self.engine.begin() as conn:
            duplicate = conn.execute(
                select(mcp_servers.c.id).where(mcp_servers.c.name == body.name)
            ).scalar_one_or_none()
            if duplicate:
                raise ConflictError(f"MCP server already exists: {body.name}")
            conn.execute(mcp_servers.insert().values(**self._values(body, server_id, now)))
        return self.get(server_id)

    async def discover(self, server_id: str) -> MCPServer:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(mcp_servers).where(mcp_servers.c.id == server_id)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError(f"MCP server not found: {server_id}")
            conn.execute(
                mcp_servers.update()
                .where(mcp_servers.c.id == server_id)
                .values(discovery_status="discovering", discovery_error="", updated_at=utcnow())
            )
        config = self._discovery_config(row)
        try:
            result = await discover_mcp_server(config)
        except Exception as exc:
            error = self._safe_discovery_error(exc, config)
            with self.engine.begin() as conn:
                conn.execute(
                    mcp_servers.update()
                    .where(mcp_servers.c.id == server_id)
                    .values(discovery_status="error", discovery_error=error, updated_at=utcnow())
                )
            return self.get(server_id)

        now = utcnow()
        with self.engine.begin() as conn:
            previous = {
                row["name"]: row
                for row in conn.execute(
                    select(mcp_tools).where(mcp_tools.c.server_id == server_id)
                ).mappings()
            }
            conn.execute(delete(mcp_tools).where(mcp_tools.c.server_id == server_id))
            self._insert_tools(conn, server_id, result.tools, now, previous=previous)
            current_ids = set(
                conn.execute(
                    select(mcp_tools.c.id).where(mcp_tools.c.server_id == server_id)
                ).scalars()
            )
            if self.phase_capabilities is not None:
                for item in previous.values():
                    if item["id"] in current_ids:
                        continue
                    self.phase_capabilities.repository.replace_capability(
                        conn,
                        capability_kind="mcp_tool",
                        capability_key=item["id"],
                        scopes=[],
                        now=now,
                    )
            conn.execute(
                mcp_servers.update()
                .where(mcp_servers.c.id == server_id)
                .values(
                    discovery_status="ready",
                    discovery_error="",
                    discovered_at=now,
                    server_info_json=json.dumps(result.server_info, ensure_ascii=False),
                    instructions=result.instructions,
                    updated_at=now,
                )
            )
        return self.get(server_id)

    def update(self, server_id: str, body: MCPServerInput) -> MCPServer:
        with self.engine.begin() as conn:
            current = conn.execute(
                select(mcp_servers).where(mcp_servers.c.id == server_id)
            ).mappings().one_or_none()
            if current is None:
                raise NotFoundError(f"MCP server not found: {server_id}")
            duplicate = conn.execute(
                select(mcp_servers.c.id).where(
                    mcp_servers.c.name == body.name,
                    mcp_servers.c.id != server_id,
                )
            ).scalar_one_or_none()
            if duplicate:
                raise ConflictError(f"MCP server already exists: {body.name}")
            values = self._values(body, server_id, utcnow(), include_id=False)
            previous_connection = tuple(
                current[key]
                for key in ("transport", "command", "args_json", "env_json", "url", "raw_config_json")
            )
            next_connection = tuple(
                values[key]
                for key in ("transport", "command", "args_json", "env_json", "url", "raw_config_json")
            )
            if previous_connection != next_connection:
                values.update(discovery_status="pending", discovery_error="")
            conn.execute(
                mcp_servers.update().where(mcp_servers.c.id == server_id).values(**values)
            )
        return self.get(server_id)

    def delete(self, server_id: str) -> None:
        with self.engine.begin() as conn:
            tool_ids = list(
                conn.execute(
                    select(mcp_tools.c.id).where(mcp_tools.c.server_id == server_id)
                ).scalars()
            )
            if not conn.execute(delete(mcp_servers).where(mcp_servers.c.id == server_id)).rowcount:
                raise NotFoundError(f"MCP server not found: {server_id}")
            if self.phase_capabilities is not None:
                for tool_id in tool_ids:
                    self.phase_capabilities.repository.replace_capability(
                        conn,
                        capability_kind="mcp_tool",
                        capability_key=tool_id,
                        scopes=[],
                        now=utcnow(),
                    )

    def replace_tools(self, server_id: str, tools: Iterable[dict[str, Any]]) -> list[MCPTool]:
        now = utcnow()
        with self.engine.begin() as conn:
            if conn.execute(
                select(mcp_servers.c.id).where(mcp_servers.c.id == server_id)
            ).scalar_one_or_none() is None:
                raise NotFoundError(f"MCP server not found: {server_id}")
            previous_ids = set(
                conn.execute(
                    select(mcp_tools.c.id).where(mcp_tools.c.server_id == server_id)
                ).scalars()
            )
            conn.execute(delete(mcp_tools).where(mcp_tools.c.server_id == server_id))
            self._insert_tools(conn, server_id, tools, now)
            current_ids = set(
                conn.execute(
                    select(mcp_tools.c.id).where(mcp_tools.c.server_id == server_id)
                ).scalars()
            )
            if self.phase_capabilities is not None:
                for tool_id in previous_ids - current_ids:
                    self.phase_capabilities.repository.replace_capability(
                        conn,
                        capability_kind="mcp_tool",
                        capability_key=tool_id,
                        scopes=[],
                        now=now,
                    )
            conn.execute(
                mcp_servers.update()
                .where(mcp_servers.c.id == server_id)
                .values(
                    discovery_status="ready",
                    discovery_error="",
                    discovered_at=now,
                    updated_at=now,
                )
            )
            return self._tools(conn, [server_id]).get(server_id, [])

    def set_tool_enabled(self, server_id: str, tool_id: str, enabled: bool) -> MCPTool:
        with self.engine.begin() as conn:
            result = conn.execute(
                mcp_tools.update()
                .where(mcp_tools.c.id == tool_id, mcp_tools.c.server_id == server_id)
                .values(enabled=enabled, updated_at=utcnow())
            )
            if not result.rowcount:
                raise NotFoundError(f"MCP tool not found: {tool_id}")
            row = conn.execute(select(mcp_tools).where(mcp_tools.c.id == tool_id)).mappings().one()
            return self._tool(row)

    def worker_binding(self, worker_name: str) -> MCPWorkerBindingView:
        worker = WorkerService(self.engine).get(worker_name)
        binding = worker.config.get("mcp") if isinstance(worker.config.get("mcp"), dict) else {}
        server_ids = [str(item) for item in binding.get("server_ids") or []]
        with self.engine.connect() as conn:
            names = list(
                conn.execute(
                    select(mcp_servers.c.name)
                    .where(mcp_servers.c.id.in_(server_ids))
                    .order_by(mcp_servers.c.name)
                ).scalars()
            ) if server_ids else []
        return MCPWorkerBindingView(
            worker_name=worker_name,
            worker_type=worker.worker_type,
            enabled=bool(binding.get("enabled", False)),
            server_ids=server_ids,
            server_names=names,
            allowed_tools=[str(item) for item in binding.get("allowed_tools") or []],
        )

    def list_worker_bindings(self) -> list[MCPWorkerBindingView]:
        return [self.worker_binding(worker.name) for worker in WorkerService(self.engine).list()]

    def save_worker_binding(
        self,
        worker_name: str,
        body: MCPWorkerBinding,
    ) -> MCPWorkerBindingView:
        del body
        # The old Worker-level endpoint remains readable for existing pages,
        # but writes are intentionally rejected.  Otherwise it would create a
        # second source of truth next to phase_capability_bindings.
        WorkerService(self.engine).get(worker_name)
        raise ConflictError(
            "具体模型后端不再持有 MCP Tool 绑定；请使用通用/Bootstrap/Reason/Explore 能力接口"
        )

    @staticmethod
    def _values(
        body: MCPServerInput,
        server_id: str,
        now: str,
        *,
        include_id: bool = True,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "name": body.name,
            "transport": body.transport,
            "command": body.command,
            "args_json": json.dumps(body.args, ensure_ascii=False),
            "env_json": json.dumps(body.env, ensure_ascii=False),
            "url": body.url,
            "source_format": body.source_format,
            "raw_config_json": json.dumps(body.raw_config, ensure_ascii=False),
            "disabled": body.disabled,
            "updated_at": now,
        }
        if include_id:
            values.update(
                id=server_id,
                discovery_status="pending",
                discovery_error="",
                discovered_at="",
                server_info_json="{}",
                instructions="",
                created_at=now,
            )
        return values

    @classmethod
    def _server(cls, row, tools: list[MCPTool]) -> MCPServer:
        return MCPServer(
            id=row["id"],
            name=row["name"],
            transport=row["transport"],
            command=row["command"],
            args=json.loads(row["args_json"] or "[]"),
            env=json.loads(row["env_json"] or "{}"),
            url=row["url"],
            source_format=row["source_format"],
            disabled=bool(row["disabled"]),
            discovery_status=row["discovery_status"],
            discovery_error=row["discovery_error"],
            discovered_at=row["discovered_at"],
            server_info=json.loads(row["server_info_json"] or "{}"),
            instructions=row["instructions"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tools=tools,
        )

    @staticmethod
    def _tools(conn, server_ids: list[str]) -> dict[str, list[MCPTool]]:
        if not server_ids:
            return {}
        rows = conn.execute(
            select(mcp_tools)
            .where(mcp_tools.c.server_id.in_(server_ids))
            .order_by(mcp_tools.c.name)
        ).mappings().all()
        result = {server_id: [] for server_id in server_ids}
        for row in rows:
            result[row["server_id"]].append(MCPService._tool(row))
        return result

    @staticmethod
    def _tool(row) -> MCPTool:
        input_schema = json.loads(row["input_schema_json"] or "{}")
        return MCPTool(
            id=row["id"],
            server_id=row["server_id"],
            name=row["name"],
            title=row["title"],
            description=row["description"],
            input_schema=input_schema,
            output_schema=json.loads(row["output_schema_json"] or "{}"),
            annotations=json.loads(row["annotations_json"] or "{}"),
            usage=_tool_usage(row["name"], input_schema),
            enabled=bool(row["enabled"]),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _insert_tools(
        conn,
        server_id: str,
        tools: Iterable[dict[str, Any]],
        now: str,
        *,
        previous: dict[str, Any] | None = None,
    ) -> None:
        previous = previous or {}
        for item in tools:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            old = previous.get(name, {})
            conn.execute(
                mcp_tools.insert().values(
                    id=old.get("id") or f"mcp_tool_{uuid.uuid4().hex[:12]}",
                    server_id=server_id,
                    name=name,
                    title=str(item.get("title", "") or ""),
                    description=str(item.get("description", "") or ""),
                    input_schema_json=json.dumps(
                        item.get("input_schema") or item.get("inputSchema") or {},
                        ensure_ascii=False,
                    ),
                    output_schema_json=json.dumps(
                        item.get("output_schema") or item.get("outputSchema") or {},
                        ensure_ascii=False,
                    ),
                    annotations_json=json.dumps(item.get("annotations") or {}, ensure_ascii=False),
                    enabled=bool(old.get("enabled", item.get("enabled", True))),
                    source=str(item.get("source", "manual") or "manual"),
                    created_at=old.get("created_at") or now,
                    updated_at=now,
                )
            )

    @staticmethod
    def _discovery_config(row) -> dict[str, Any]:
        return {
            "command": row["command"],
            "args": json.loads(row["args_json"] or "[]"),
            "env": json.loads(row["env_json"] or "{}"),
            "url": row["url"],
            "transport": row["transport"],
            "raw_config": json.loads(row["raw_config_json"] or "{}"),
        }

    @staticmethod
    def _runtime_config(row) -> dict[str, Any]:
        if row["command"]:
            return {
                "command": row["command"],
                "args": json.loads(row["args_json"] or "[]"),
                "env": json.loads(row["env_json"] or "{}"),
            }
        config = {"url": row["url"], "transport": row["transport"]}
        raw = json.loads(row["raw_config_json"] or "{}")
        if raw.get("headers"):
            config["headers"] = raw["headers"]
        return config

    @staticmethod
    def _safe_discovery_error(exc: Exception, config: dict[str, Any]) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        secrets = [str(value) for value in (config.get("env") or {}).values() if len(str(value)) >= 6]
        raw = config.get("raw_config") or {}
        headers = raw.get("headers") if isinstance(raw, dict) else {}
        if isinstance(headers, dict):
            secrets.extend(str(value) for value in headers.values() if len(str(value)) >= 6)
        for secret in secrets:
            message = message.replace(secret, "***")
        message = re.sub(
            r"((?:[?&\s]|\b)(?:token|key|secret|auth|authorization|password|api[_-]?key)\s*=\s*)[^&\s]+",
            r"\1***",
            message,
            flags=re.I,
        )
        message = re.sub(
            r"\bBearer\s+[^\s,;]+",
            "Bearer ***",
            message,
            flags=re.I,
        )
        return message[-3_000:]


def _tool_usage(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    params: list[str] = []
    for key, value in properties.items():
        item = value if isinstance(value, dict) else {}
        kind = item.get("type") or "any"
        if isinstance(kind, list):
            kind = " | ".join(str(part) for part in kind)
        params.append(f"{key}{'' if key in required else '?'}: {kind}")
    return f"{name}({', '.join(params)})"
