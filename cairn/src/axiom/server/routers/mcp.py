import json
import logging
import sqlite3
import threading
import uuid
from queue import Empty, Queue
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from fastapi import APIRouter, Body, HTTPException, status

from axiom.server.dispatch_config_store import bump_dispatch_config_version
from axiom.server.db import get_conn
from axiom.server.models import (
    MCPServer,
    MCPServerCreate,
    MCPServerTool,
    MCPServerToolCreate,
    MCPServerToolUpdate,
    MCPServerUpdate,
)

LOG = logging.getLogger(__name__)
MCP_SYNC_TIMEOUT_SECONDS = 15


def _csv_items(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _csv_join(items: list[str]) -> str:
    return ", ".join(dict.fromkeys(item for item in items if item))


def _remove_scoped_tool_tokens(raw: str, server_name: str) -> str:
    prefix = f"{server_name}:"
    slash_prefix = f"{server_name}/"
    return _csv_join([
        item for item in _csv_items(raw)
        if not item.startswith(prefix) and not item.startswith(slash_prefix)
    ])


def _rename_scoped_tool_tokens(raw: str, old_name: str, new_name: str) -> str:
    renamed: list[str] = []
    for item in _csv_items(raw):
        if item.startswith(f"{old_name}:"):
            renamed.append(f"{new_name}:{item.split(':', 1)[1]}")
        elif item.startswith(f"{old_name}/"):
            renamed.append(f"{new_name}/{item.split('/', 1)[1]}")
        else:
            renamed.append(item)
    return _csv_join(renamed)


def _update_worker_mcp_envs(conn, updater) -> None:
    rows = conn.execute("SELECT name, env_json FROM dispatch_workers").fetchall()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in rows:
        try:
            env = json.loads(row["env_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            env = {}
        if not isinstance(env, dict):
            env = {}
        before = json.dumps(env, ensure_ascii=False, sort_keys=True)
        updater(env)
        after = json.dumps(env, ensure_ascii=False, sort_keys=True)
        if after == before:
            continue
        conn.execute(
            "UPDATE dispatch_workers SET env_json = ?, updated_at = ? WHERE name = ?",
            (json.dumps(env, ensure_ascii=False, separators=(",", ":")), now, row["name"]),
        )


def _remove_server_from_worker_mcp_allowlists(conn, server_name: str) -> None:
    def updater(env: dict[str, Any]) -> None:
        env["PI_MCP_ALLOWED_SERVERS"] = _csv_join([
            item for item in _csv_items(str(env.get("PI_MCP_ALLOWED_SERVERS", "")))
            if item != server_name
        ])
        env["PI_MCP_ALLOWED_TOOLS"] = _remove_scoped_tool_tokens(
            str(env.get("PI_MCP_ALLOWED_TOOLS", "")),
            server_name,
        )

    _update_worker_mcp_envs(conn, updater)


def _rename_server_in_worker_mcp_allowlists(conn, old_name: str, new_name: str) -> None:
    def updater(env: dict[str, Any]) -> None:
        env["PI_MCP_ALLOWED_SERVERS"] = _csv_join([
            new_name if item == old_name else item
            for item in _csv_items(str(env.get("PI_MCP_ALLOWED_SERVERS", "")))
        ])
        env["PI_MCP_ALLOWED_TOOLS"] = _rename_scoped_tool_tokens(
            str(env.get("PI_MCP_ALLOWED_TOOLS", "")),
            old_name,
            new_name,
        )

    _update_worker_mcp_envs(conn, updater)


def _mark_dispatch_config_changed() -> None:
    bump_dispatch_config_version()



def _expand_stored_payload(row: dict) -> dict:
    """Normalise row for API response: ensure url is always present
    (NULL becomes None for the response model).
    """
    row = dict(row)
    row["url"] = row.get("url")
    row.setdefault("tools", [])
    return row


def _tool_payload(row: dict) -> dict:
    payload = dict(row)
    payload["enabled"] = bool(payload.get("enabled"))
    return payload


def _load_tools_by_server(conn, server_ids: list[str]) -> dict[str, list[dict]]:
    if not server_ids:
        return {}
    placeholders = ",".join("?" for _ in server_ids)
    rows = conn.execute(
        f"SELECT * FROM mcp_server_tools WHERE server_id IN ({placeholders}) ORDER BY name ASC",
        server_ids,
    ).fetchall()
    grouped: dict[str, list[dict]] = {server_id: [] for server_id in server_ids}
    for row in rows:
        grouped.setdefault(row["server_id"], []).append(_tool_payload(dict(row)))
    return grouped


def _iter_sse_events(response):
    event = "message"
    data_lines: list[str] = []
    for line in response.iter_lines(decode_unicode=True):
        if line is None:
            continue
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())


def _post_mcp_json(session: requests.Session, endpoint: str, payload: dict[str, Any]) -> None:
    resp = session.post(
        endpoint,
        json=payload,
        headers={"Accept": "application/json, text/event-stream"},
        timeout=(3, 10),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"MCP message POST failed: HTTP {resp.status_code} {resp.text[:200]}")


def _wait_mcp_response(events: Queue, request_id: int, timeout: float) -> dict[str, Any]:
    deadline = datetime.now(timezone.utc).timestamp() + timeout
    while True:
        remaining = deadline - datetime.now(timezone.utc).timestamp()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for MCP response id={request_id}")
        try:
            payload = events.get(timeout=min(remaining, 1.0))
        except Empty:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("id") != request_id:
            continue
        if payload.get("error"):
            raise RuntimeError(f"MCP error for id={request_id}: {payload['error']}")
        return payload


def _list_mcp_tools_via_sse(url: str) -> list[dict[str, str]]:
    session = requests.Session()
    # Local MCP servers often live behind 127.0.0.1 and should not be routed
    # through user/system HTTP proxy settings.
    session.trust_env = False
    events: Queue = Queue()
    stream_response = None
    stop = threading.Event()

    try:
        stream_response = session.get(
            url,
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=(3, None),
        )
        if stream_response.status_code >= 400:
            raise RuntimeError(f"MCP SSE connect failed: HTTP {stream_response.status_code}")

        endpoint = ""
        iterator = _iter_sse_events(stream_response)
        for event, data in iterator:
            if event == "endpoint":
                endpoint = urljoin(url, data.strip())
                break
        if not endpoint:
            raise RuntimeError("MCP SSE endpoint event not received")

        def reader() -> None:
            try:
                for event, data in iterator:
                    if stop.is_set():
                        break
                    if event not in {"message", "response"}:
                        continue
                    try:
                        events.put(json.loads(data))
                    except json.JSONDecodeError:
                        LOG.debug("ignored non-json MCP SSE data: %r", data[:200])
            except Exception as exc:  # pragma: no cover - best effort background reader
                if not stop.is_set():
                    events.put({"_reader_error": str(exc)})

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        initialize_id = 1
        _post_mcp_json(session, endpoint, {
            "jsonrpc": "2.0",
            "id": initialize_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "axiom", "version": "0.2.1"},
            },
        })
        _wait_mcp_response(events, initialize_id, MCP_SYNC_TIMEOUT_SECONDS)
        _post_mcp_json(session, endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools: list[dict[str, str]] = []
        cursor = None
        request_id = 2
        while True:
            params = {"cursor": cursor} if cursor else {}
            _post_mcp_json(session, endpoint, {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/list",
                "params": params,
            })
            response = _wait_mcp_response(events, request_id, MCP_SYNC_TIMEOUT_SECONDS)
            result = response.get("result") or {}
            for item in result.get("tools") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                description = str(item.get("description") or "").strip()
                tools.append({"name": name, "description": description})
            cursor = result.get("nextCursor")
            if not cursor:
                break
            request_id += 1
        return tools
    finally:
        stop.set()
        if stream_response is not None:
            stream_response.close()
        session.close()


def _discover_mcp_tools(server: sqlite3.Row) -> list[dict[str, str]]:
    url = (server["url"] or "").strip() if "url" in server.keys() else ""
    if url:
        return _list_mcp_tools_via_sse(url)
    raise RuntimeError("Only URL-based MCP servers can be auto-synced right now")


def _sync_mcp_server_tools(server_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        server = _get_server(conn, server_id)
        server_payload = dict(server)

    discovered_tools = _discover_mcp_tools(server)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    deduped: dict[str, str] = {}
    for tool in discovered_tools:
        name = (tool.get("name") or "").strip()
        if not name:
            continue
        deduped[name] = (tool.get("description") or "").strip()
    if not deduped:
        raise RuntimeError("MCP tools sync returned no tools")

    with get_conn() as conn:
        server = _get_server(conn, server_id)
        for name, description in deduped.items():
            tool_id = f"mcp_tool_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO mcp_server_tools
                    (id, server_id, server_name, name, description, enabled, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 'sync', ?, ?)
                ON CONFLICT(server_id, name) DO UPDATE SET
                    server_name = excluded.server_name,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (tool_id, server_id, server["name"], name, description, now, now),
            )
        rows = conn.execute(
            "SELECT * FROM mcp_server_tools WHERE server_id = ? ORDER BY name ASC",
            (server_id,),
        ).fetchall()
        return [_tool_payload(dict(row)) for row in rows]


def _try_sync_mcp_server_tools(server_id: str) -> list[dict[str, Any]]:
    try:
        return _sync_mcp_server_tools(server_id)
    except Exception as exc:
        LOG.warning("failed to auto-sync MCP tools server_id=%s: %s", server_id, exc)
        return []


def _get_server(conn, server_id: str):
    row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return row


def _serialise_stored_payload(command, url, args_json, env_json):
    """Normalise incoming fields for DB.

    DB columns: command TEXT (defaults to ''), url TEXT (NULLABLE), args_json TEXT.
    command/url both go in their own columns directly.  args_json unchanged.
    """
    return command or "", url, args_json or "[]", env_json or "{}"


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/servers", response_model=list[MCPServer])
def list_mcp_servers():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM mcp_servers ORDER BY created_at DESC").fetchall()
        server_ids = [r["id"] for r in rows]
        tools_by_server = _load_tools_by_server(conn, server_ids)
        payload = []
        for row in rows:
            item = _expand_stored_payload(dict(row))
            item["tools"] = tools_by_server.get(row["id"], [])
            payload.append(item)
        return payload


@router.post("/servers", response_model=MCPServer, status_code=status.HTTP_201_CREATED)
def create_mcp_server(req: MCPServerCreate):
    server_id = f"mcp_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stored_command, stored_url, stored_args, stored_env = _serialise_stored_payload(
        req.command, req.url, req.args_json, req.env_json
    )

    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM mcp_servers WHERE name = ?", (req.name,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="MCP server with this name already exists")

        conn.execute(
            """
            INSERT INTO mcp_servers (id, name, command, args_json, env_json, disabled, url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_id,
                req.name,
                stored_command,
                stored_args,
                stored_env,
                1 if req.disabled else 0,
                stored_url,
                now,
                now,
            ),
        )

    tools = _try_sync_mcp_server_tools(server_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        payload = _expand_stored_payload(dict(row))
        payload["tools"] = tools or _load_tools_by_server(conn, [server_id]).get(server_id, [])
    _mark_dispatch_config_changed()
    return payload


@router.put("/servers/{server_id}", response_model=MCPServer)
def update_mcp_server(server_id: str, req: MCPServerUpdate):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")

        updates = []
        params = []
        if req.name is not None and req.name != row["name"]:
            existing = conn.execute("SELECT id FROM mcp_servers WHERE name = ? AND id != ?", (req.name, server_id)).fetchone()
            if existing:
                raise HTTPException(status_code=400, detail="MCP server with this name already exists")
            updates.append("name = ?")
            params.append(req.name)
        if req.command is not None:
            updates.append("command = ?")
            params.append(req.command)
        if req.url is not None:
            updates.append("url = ?")
            params.append(req.url)
        if req.args_json is not None:
            updates.append("args_json = ?")
            params.append(req.args_json)
        if req.env_json is not None:
            updates.append("env_json = ?")
            params.append(req.env_json)
        if req.disabled is not None:
            updates.append("disabled = ?")
            params.append(1 if req.disabled else 0)

        old_name = row["name"]
        new_name = req.name if req.name is not None and req.name != row["name"] else ""

        if not updates:
            return _expand_stored_payload(dict(row))

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updates.append("updated_at = ?")
        params.append(now)
        params.append(server_id)

        conn.execute(f"UPDATE mcp_servers SET {', '.join(updates)} WHERE id = ?", params)
        if new_name:
            conn.execute(
                "UPDATE mcp_server_tools SET server_name = ?, updated_at = ? WHERE server_id = ?",
                (new_name, now, server_id),
            )
            _rename_server_in_worker_mcp_allowlists(conn, old_name, new_name)
    tools = _try_sync_mcp_server_tools(server_id)
    with get_conn() as conn:
        updated_row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        payload = _expand_stored_payload(dict(updated_row))
        payload["tools"] = tools or _load_tools_by_server(conn, [server_id]).get(server_id, [])
    _mark_dispatch_config_changed()
    return payload


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(server_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT id, name FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")
        _remove_server_from_worker_mcp_allowlists(conn, row["name"])
        conn.execute("DELETE FROM mcp_server_tools WHERE server_id = ?", (server_id,))
        conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
    _mark_dispatch_config_changed()


@router.get("/servers/{server_id}/tools", response_model=list[MCPServerTool])
def list_mcp_server_tools(server_id: str):
    with get_conn() as conn:
        _get_server(conn, server_id)
        rows = conn.execute(
            "SELECT * FROM mcp_server_tools WHERE server_id = ? ORDER BY name ASC",
            (server_id,),
        ).fetchall()
        return [_tool_payload(dict(row)) for row in rows]


@router.post("/servers/{server_id}/tools", response_model=MCPServerTool, status_code=status.HTTP_201_CREATED)
def create_mcp_server_tool(server_id: str, req: MCPServerToolCreate):
    tool_id = f"mcp_tool_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        server = _get_server(conn, server_id)
        try:
            conn.execute(
                """
                INSERT INTO mcp_server_tools
                    (id, server_id, server_name, name, description, enabled, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_id,
                    server_id,
                    server["name"],
                    req.name,
                    req.description,
                    1 if req.enabled else 0,
                    req.source or "manual",
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="MCP tool with this name already exists") from exc
        row = conn.execute("SELECT * FROM mcp_server_tools WHERE id = ?", (tool_id,)).fetchone()
        payload = _tool_payload(dict(row))
    _mark_dispatch_config_changed()
    return payload


@router.put("/servers/{server_id}/tools/{tool_id}", response_model=MCPServerTool)
def update_mcp_server_tool(server_id: str, tool_id: str, req: MCPServerToolUpdate):
    with get_conn() as conn:
        _get_server(conn, server_id)
        row = conn.execute(
            "SELECT * FROM mcp_server_tools WHERE id = ? AND server_id = ?",
            (tool_id, server_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP tool not found")
        updates = []
        params: list[Any] = []
        if req.name is not None and req.name != row["name"]:
            updates.append("name = ?")
            params.append(req.name)
        if req.description is not None:
            updates.append("description = ?")
            params.append(req.description)
        if req.enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if req.enabled else 0)
        if req.source is not None:
            updates.append("source = ?")
            params.append(req.source or "manual")
        if not updates:
            return _tool_payload(dict(row))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updates.append("updated_at = ?")
        params.extend([now, tool_id, server_id])
        try:
            conn.execute(
                f"UPDATE mcp_server_tools SET {', '.join(updates)} WHERE id = ? AND server_id = ?",
                params,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="MCP tool with this name already exists") from exc
        updated = conn.execute(
            "SELECT * FROM mcp_server_tools WHERE id = ? AND server_id = ?",
            (tool_id, server_id),
        ).fetchone()
        payload = _tool_payload(dict(updated))
    _mark_dispatch_config_changed()
    return payload


@router.delete("/servers/{server_id}/tools/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server_tool(server_id: str, tool_id: str):
    with get_conn() as conn:
        _get_server(conn, server_id)
        row = conn.execute(
            "SELECT id FROM mcp_server_tools WHERE id = ? AND server_id = ?",
            (tool_id, server_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP tool not found")
        conn.execute("DELETE FROM mcp_server_tools WHERE id = ? AND server_id = ?", (tool_id, server_id))
    _mark_dispatch_config_changed()


def _extract_import_tool_names(payload: Any) -> list[str]:
    raw_tools: Any
    if isinstance(payload, dict):
        raw_tools = payload.get("tools", payload.get("names", payload.get("text", "")))
    else:
        raw_tools = payload
    if isinstance(raw_tools, str):
        parts = []
        for line in raw_tools.replace(",", "\n").splitlines():
            name = line.strip()
            if name:
                parts.append(name)
        return parts
    if isinstance(raw_tools, list):
        names = []
        for item in raw_tools:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
            else:
                name = ""
            if name:
                names.append(name)
        return names
    return []


@router.post("/servers/{server_id}/tools/import", response_model=list[MCPServerTool])
def import_mcp_server_tools(server_id: str, payload: Any = Body(...)):
    names = list(dict.fromkeys(_extract_import_tool_names(payload)))
    if not names:
        raise HTTPException(status_code=400, detail="No tool names found")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        server = _get_server(conn, server_id)
        for name in names:
            tool_id = f"mcp_tool_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT OR IGNORE INTO mcp_server_tools
                    (id, server_id, server_name, name, description, enabled, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', 1, 'import', ?, ?)
                """,
                (tool_id, server_id, server["name"], name, now, now),
            )
        rows = conn.execute(
            "SELECT * FROM mcp_server_tools WHERE server_id = ? ORDER BY name ASC",
            (server_id,),
        ).fetchall()
        payload = [_tool_payload(dict(row)) for row in rows]
    _mark_dispatch_config_changed()
    return payload


@router.post("/servers/{server_id}/tools/sync", response_model=list[MCPServerTool])
def sync_mcp_server_tools(server_id: str):
    try:
        payload = _sync_mcp_server_tools(server_id)
        _mark_dispatch_config_changed()
        return payload
    except Exception as exc:
        LOG.warning("failed to sync MCP tools server_id=%s: %s", server_id, exc)
        raise HTTPException(status_code=502, detail=f"MCP tools sync failed: {exc}") from exc
