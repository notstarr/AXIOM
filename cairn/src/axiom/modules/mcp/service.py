from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from typing import Any

import tomllib
import yaml
from sqlalchemy import Engine, delete, select

from axiom.infrastructure.database.metadata import mcp_server_tools, mcp_servers
from axiom.modules.common.time import utcnow
from axiom.modules.mcp.discovery import discover_mcp_server
from axiom.modules.mcp.schemas import MCPServer, MCPServerInput, MCPTool
from axiom.modules.workers.schemas import UpsertWorker
from axiom.modules.workers.service import WorkerService


def parse_mcp_config(content: str, format: str = "auto") -> tuple[str, list[MCPServerInput]]:
    """Parse common MCP config dialects into one canonical list.

    Accepted input is intentionally forgiving: standard mcpServers JSON/YAML,
    one-server JSON, and Codex TOML's [mcp_servers.name] form are all reduced
    to the same stdio/http representation before persistence.
    """
    raw = content.strip()
    if not raw:
        raise ValueError("MCP 配置不能为空")
    fmt = format.lower().strip()
    if fmt == "auto":
        if raw.startswith("{") or raw.startswith("["):
            fmt = "json"
        elif "[mcp_servers" in raw or "[mcpServers" in raw:
            fmt = "toml"
        else:
            fmt = "yaml"
    try:
        payload = json.loads(raw) if fmt == "json" else tomllib.loads(raw) if fmt == "toml" else yaml.safe_load(raw)
    except Exception as exc:
        raise ValueError(f"MCP {fmt} 格式解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("MCP 配置必须是对象")

    wrapper = payload.get("mcpServers", payload.get("mcp_servers"))
    if isinstance(wrapper, dict):
        entries = [(str(name), cfg) for name, cfg in wrapper.items()]
    elif isinstance(payload.get("servers"), dict):
        entries = [(str(name), cfg) for name, cfg in payload["servers"].items()]
    elif any(key in payload for key in ("command", "url", "endpoint", "transport")):
        entries = [(str(payload.get("name") or "server"), payload)]
    else:
        # A TOML file may contain a single named table without a wrapper.
        entries = [(str(name), cfg) for name, cfg in payload.items() if isinstance(cfg, dict)]
    if not entries:
        raise ValueError("没有找到 MCP Server 配置")

    result: list[MCPServerInput] = []
    for name, cfg in entries:
        if not isinstance(cfg, dict):
            raise ValueError(f"MCP Server {name} 必须是对象")
        cfg = dict(cfg)
        name = str(cfg.pop("name", name)).strip()
        command = str(cfg.pop("command", "") or "").strip()
        url = str(cfg.pop("url", cfg.pop("endpoint", "")) or "").strip()
        transport = str(cfg.pop("transport", "") or "").lower().strip()
        args = cfg.pop("args", []) or []
        env = cfg.pop("env", {}) or {}
        if not isinstance(args, list):
            raise ValueError(f"MCP Server {name} 的 args 必须是数组")
        if not isinstance(env, dict):
            raise ValueError(f"MCP Server {name} 的 env 必须是对象")
        if not command and not url:
            raise ValueError(f"MCP Server {name} 必须提供 command 或 url")
        if not transport:
            transport = "stdio" if command else "sse" if url.endswith("/sse") else "http"
        result.append(MCPServerInput(
            name=name, command=command, args=args, env=env, url=url,
            transport=transport, source_format=fmt, raw_config={**cfg, **{"command": command, "args": args, "env": env, "url": url}},
        ))
    return fmt, result


class MCPService:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list(self) -> list[MCPServer]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(mcp_servers).order_by(mcp_servers.c.name)).mappings().all()
            tools = self._tools(conn, [row["id"] for row in rows])
            return [self._server(dict(row), tools.get(row["id"], [])) for row in rows]

    def get(self, server_id: str) -> MCPServer:
        with self.engine.connect() as conn:
            row = conn.execute(select(mcp_servers).where(mcp_servers.c.id == server_id)).mappings().one_or_none()
            if row is None:
                raise ValueError(f"MCP Server not found: {server_id}")
            return self._server(dict(row), self._tools(conn, [server_id]).get(server_id, []))

    def create(self, body: MCPServerInput) -> MCPServer:
        now = utcnow()
        server_id = f"mcp_{uuid.uuid4().hex[:12]}"
        values = self._values(body, server_id, now)
        with self.engine.begin() as conn:
            if conn.execute(select(mcp_servers.c.id).where(mcp_servers.c.name == body.name)).scalar_one_or_none():
                raise ValueError(f"MCP Server 已存在：{body.name}")
            conn.execute(mcp_servers.insert().values(**values))
            return self._server(dict(conn.execute(select(mcp_servers).where(mcp_servers.c.id == server_id)).mappings().one()), [])

    async def discover(self, server_id: str) -> MCPServer:
        """Discover and persist the server's advertised tools without deleting old tools on failure."""
        with self.engine.begin() as conn:
            row = conn.execute(select(mcp_servers).where(mcp_servers.c.id == server_id)).mappings().one_or_none()
            if row is None:
                raise ValueError(f"MCP Server not found: {server_id}")
            conn.execute(mcp_servers.update().where(mcp_servers.c.id == server_id).values(
                discovery_status="discovering", discovery_error="", updated_at=utcnow(),
            ))
        config = self._discovery_config(dict(row))
        try:
            result = await discover_mcp_server(config)
        except Exception as exc:
            error = self._safe_discovery_error(exc, config)
            with self.engine.begin() as conn:
                conn.execute(mcp_servers.update().where(mcp_servers.c.id == server_id).values(
                    discovery_status="error", discovery_error=error, updated_at=utcnow(),
                ))
            return self.get(server_id)

        now = utcnow()
        with self.engine.begin() as conn:
            existing = {
                item["name"]: dict(item)
                for item in conn.execute(select(mcp_server_tools).where(mcp_server_tools.c.server_id == server_id)).mappings()
            }
            conn.execute(delete(mcp_server_tools).where(mcp_server_tools.c.server_id == server_id))
            self._insert_tools(conn, server_id, result.tools, now, existing=existing)
            conn.execute(mcp_servers.update().where(mcp_servers.c.id == server_id).values(
                discovery_status="ready", discovery_error="", discovered_at=now,
                server_info_json=json.dumps(result.server_info, ensure_ascii=False),
                instructions=result.instructions, updated_at=now,
            ))
        return self.get(server_id)

    def update(self, server_id: str, body: MCPServerInput) -> MCPServer:
        now = utcnow()
        with self.engine.begin() as conn:
            current = conn.execute(select(mcp_servers).where(mcp_servers.c.id == server_id)).mappings().one_or_none()
            if current is None:
                raise ValueError(f"MCP Server not found: {server_id}")
            duplicate = conn.execute(select(mcp_servers.c.id).where(mcp_servers.c.name == body.name, mcp_servers.c.id != server_id)).scalar_one_or_none()
            if duplicate:
                raise ValueError(f"MCP Server 已存在：{body.name}")
            values = self._values(body, server_id, now, include_id=False)
            previous_connection = (
                current["command"], current["args_json"], current["env_json"], current["url"], current["transport"], current["raw_config"]
            )
            next_connection = (
                values["command"], values["args_json"], values["env_json"], values["url"], values["transport"], values["raw_config"]
            )
            if previous_connection != next_connection:
                values.update(discovery_status="pending", discovery_error="")
            conn.execute(mcp_servers.update().where(mcp_servers.c.id == server_id).values(**values))
            row = conn.execute(select(mcp_servers).where(mcp_servers.c.id == server_id)).mappings().one()
            return self._server(dict(row), self._tools(conn, [server_id]).get(server_id, []))

    def delete(self, server_id: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(mcp_servers).where(mcp_servers.c.id == server_id)).rowcount:
                raise ValueError(f"MCP Server not found: {server_id}")

    def replace_tools(self, server_id: str, tools: Iterable[dict[str, Any]]) -> list[MCPTool]:
        now = utcnow()
        with self.engine.begin() as conn:
            if conn.execute(select(mcp_servers.c.id).where(mcp_servers.c.id == server_id)).scalar_one_or_none() is None:
                raise ValueError(f"MCP Server not found: {server_id}")
            conn.execute(delete(mcp_server_tools).where(mcp_server_tools.c.server_id == server_id))
            self._insert_tools(conn, server_id, tools, now)
            return self._tools(conn, [server_id]).get(server_id, [])

    def set_tool_enabled(self, server_id: str, tool_id: str, enabled: bool) -> MCPTool:
        with self.engine.begin() as conn:
            row = conn.execute(select(mcp_server_tools).where(
                mcp_server_tools.c.id == tool_id, mcp_server_tools.c.server_id == server_id,
            )).mappings().one_or_none()
            if row is None:
                raise ValueError(f"MCP Tool not found: {tool_id}")
            conn.execute(mcp_server_tools.update().where(mcp_server_tools.c.id == tool_id).values(
                enabled=enabled, updated_at=utcnow(),
            ))
            updated = conn.execute(select(mcp_server_tools).where(mcp_server_tools.c.id == tool_id)).mappings().one()
            return self._tool(dict(updated))

    def worker_binding(self, worker_name: str) -> dict[str, Any]:
        worker = WorkerService(self.engine).get(worker_name)
        env = worker.config.get("env") if isinstance(worker.config.get("env"), dict) else worker.config
        raw = str(env.get("PI_MCP_CONFIG_JSON", "") or "")
        names: list[str] = []
        if raw:
            try: names = list((json.loads(raw).get("mcpServers") or {}).keys())
            except (TypeError, ValueError): pass
        if not names:
            names = _csv(env.get("PI_MCP_ALLOWED_SERVERS", ""))
        return {"worker": worker_name, "enabled": str(env.get("PI_MCP_ENABLED", "false")).lower() == "true", "server_names": names, "allowed_tools": _csv(env.get("PI_MCP_ALLOWED_TOOLS", ""))}

    def save_worker_binding(self, worker_name: str, enabled: bool, server_ids: list[str], allowed_tools: list[str]) -> dict[str, Any]:
        worker = WorkerService(self.engine).get(worker_name)
        with self.engine.connect() as conn:
            rows = conn.execute(select(mcp_servers).where(mcp_servers.c.id.in_(server_ids), mcp_servers.c.disabled.is_(False))).mappings().all() if server_ids else []
        selected = {row["name"]: self._config(dict(row)) for row in rows}
        env = dict(worker.config.get("env")) if isinstance(worker.config.get("env"), dict) else dict(worker.config)
        env["PI_MCP_ENABLED"] = "true" if enabled else "false"
        env["PI_MCP_CONFIG_JSON"] = json.dumps({"mcpServers": selected}, ensure_ascii=False, separators=(",", ":"))
        env["PI_MCP_ALLOWED_SERVERS"] = ", ".join(selected)
        env["PI_MCP_ALLOWED_TOOLS"] = ", ".join(dict.fromkeys(allowed_tools))
        config = dict(worker.config)
        if isinstance(config.get("env"), dict): config["env"] = env
        else: config = env
        WorkerService(self.engine).upsert(UpsertWorker(name=worker.name, worker_type=worker.worker_type, capabilities=worker.capabilities, max_running=worker.max_running, priority=worker.priority, config=config, disabled=worker.disabled))
        return self.worker_binding(worker_name)

    @staticmethod
    def _values(body: MCPServerInput, server_id: str, now: str, *, include_id: bool = True) -> dict[str, Any]:
        values = {"name": body.name, "transport": body.transport or ("stdio" if body.command else "http"), "command": body.command, "args_json": json.dumps(body.args, ensure_ascii=False), "env_json": json.dumps(body.env, ensure_ascii=False), "url": body.url, "source_format": body.source_format, "raw_config": json.dumps(body.raw_config or {}, ensure_ascii=False), "disabled": body.disabled, "updated_at": now}
        if include_id:
            values["created_at"] = now
            values.update(discovery_status="pending", discovery_error="", discovered_at="", server_info_json="{}", instructions="")
        if include_id: values["id"] = server_id
        return values

    @staticmethod
    def _config(row: dict[str, Any]) -> dict[str, Any]:
        result = {"command": row["command"], "args": json.loads(row["args_json"] or "[]"), "env": json.loads(row["env_json"] or "{}")}
        if row["url"]: result["url"] = row["url"]
        return result

    @classmethod
    def _server(cls, row: dict[str, Any], tools: list[MCPTool]) -> MCPServer:
        return MCPServer(
            id=row["id"], name=row["name"], transport=row["transport"], command=row["command"],
            args=json.loads(row["args_json"] or "[]"), env=json.loads(row["env_json"] or "{}"),
            url=row["url"], source_format=row["source_format"], disabled=bool(row["disabled"]),
            discovery_status=row.get("discovery_status") or "pending", discovery_error=row.get("discovery_error") or "",
            discovered_at=row.get("discovered_at") or "", server_info=json.loads(row.get("server_info_json") or "{}"),
            instructions=row.get("instructions") or "", created_at=row["created_at"], updated_at=row["updated_at"], tools=tools,
        )

    @staticmethod
    def _tools(conn, server_ids: list[str]) -> dict[str, list[MCPTool]]:
        if not server_ids: return {}
        rows = conn.execute(select(mcp_server_tools).where(mcp_server_tools.c.server_id.in_(server_ids)).order_by(mcp_server_tools.c.name)).mappings().all()
        return {server_id: [MCPService._tool(dict(row)) for row in rows if row["server_id"] == server_id] for server_id in server_ids}

    @staticmethod
    def _tool(row: dict[str, Any]) -> MCPTool:
        schema = json.loads(row.get("input_schema_json") or "{}")
        return MCPTool(
            id=row["id"], server_id=row["server_id"], name=row["name"], title=row.get("title") or "",
            description=row.get("description") or "", input_schema=schema,
            output_schema=json.loads(row.get("output_schema_json") or "{}"),
            annotations=json.loads(row.get("annotations_json") or "{}"),
            usage=_tool_usage(row["name"], schema), enabled=bool(row["enabled"]), source=row["source"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _insert_tools(conn, server_id: str, tools: Iterable[dict[str, Any]], now: str, *, existing: dict[str, dict[str, Any]] | None = None) -> None:
        previous = existing or {}
        for item in tools:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            old = previous.get(name, {})
            conn.execute(mcp_server_tools.insert().values(
                id=old.get("id") or f"mcp_tool_{uuid.uuid4().hex[:12]}", server_id=server_id, name=name,
                title=str(item.get("title", "") or ""), description=str(item.get("description", "") or ""),
                input_schema_json=json.dumps(item.get("input_schema") or item.get("inputSchema") or {}, ensure_ascii=False),
                output_schema_json=json.dumps(item.get("output_schema") or item.get("outputSchema") or {}, ensure_ascii=False),
                annotations_json=json.dumps(item.get("annotations") or {}, ensure_ascii=False),
                enabled=bool(old.get("enabled", item.get("enabled", True))),
                source=str(item.get("source", "manual") or "manual"),
                created_at=old.get("created_at") or now, updated_at=now,
            ))

    @staticmethod
    def _discovery_config(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "command": row.get("command") or "", "args": json.loads(row.get("args_json") or "[]"),
            "env": json.loads(row.get("env_json") or "{}"), "url": row.get("url") or "",
            "transport": row.get("transport") or "", "raw_config": json.loads(row.get("raw_config") or "{}"),
        }

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
        message = re.sub(r"([?&](?:token|key|secret|auth)=[^&\s]+)", lambda match: match.group(0).split("=")[0] + "=***", message, flags=re.I)
        if "All connection attempts failed" in message:
            message = f"无法连接 MCP Server；请确认服务已启动且地址可达。{message}"
        return message[-3000:]


def _csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").replace("\n", ",").split(",") if item.strip()]


def _tool_usage(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    params: list[str] = []
    for key, value in properties.items():
        item = value if isinstance(value, dict) else {}
        kind = item.get("type") or "any"
        if isinstance(kind, list):
            kind = " | ".join(str(part) for part in kind)
        suffix = "" if key in required else "?"
        params.append(f"{key}{suffix}: {kind}")
    return f"{name}({', '.join(params)})"
