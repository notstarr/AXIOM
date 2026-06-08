from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import CreatePiToolRequest, PiTool, UpdatePiToolRequest
from cairn.server.services import utcnow

router = APIRouter(tags=["tools"])

PI_BUILTIN_TOOLS: tuple[dict[str, object], ...] = (
    {
        "name": "read",
        "display_name": "Read",
        "description": "读取文件内容，供 PI 查看目标文件或上下文。",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "write",
        "display_name": "Write",
        "description": "写入新文件内容。",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
    },
    {
        "name": "edit",
        "display_name": "Edit",
        "description": "编辑已有文件。",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}},
    },
    {
        "name": "bash",
        "display_name": "Bash",
        "description": "执行本地 shell 命令，受策略和黑名单约束。",
        "schema": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
    {
        "name": "grep",
        "display_name": "Grep",
        "description": "搜索文本内容。",
        "schema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
    },
    {
        "name": "find",
        "display_name": "Find",
        "description": "查找文件路径。",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string"}}},
    },
    {
        "name": "ls",
        "display_name": "List",
        "description": "列出目录内容。",
        "schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
)


@router.get("/tools", response_model=list[PiTool])
def list_tools():
    with get_conn() as conn:
        _sync_builtin_tools(conn)
        rows = conn.execute("SELECT * FROM pi_tools ORDER BY builtin DESC, name ASC").fetchall()
        return [_tool_from_row(row) for row in rows]


@router.post("/tools", response_model=PiTool, status_code=201)
def create_tool(body: CreatePiToolRequest):
    now = utcnow()
    tool_id = f"tool_{uuid.uuid4().hex[:12]}"
    display_name = body.display_name or body.name
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM pi_tools WHERE name = ?", (body.name,)).fetchone()
        if existing is not None:
            raise HTTPException(409, "Tool name already exists")
        conn.execute(
            """
            INSERT INTO pi_tools (
                id, name, display_name, description, enabled, builtin, source,
                schema_json, config_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'custom', ?, ?, ?, ?)
            """,
            (
                tool_id,
                body.name,
                display_name,
                body.description,
                1 if body.enabled else 0,
                json.dumps(body.schema_, ensure_ascii=False, separators=(",", ":")),
                json.dumps(body.config, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM pi_tools WHERE id = ?", (tool_id,)).fetchone()
        assert row is not None
        return _tool_from_row(row)


@router.post("/tools/sync-pi", response_model=list[PiTool])
def sync_pi_tools():
    with get_conn() as conn:
        _sync_builtin_tools(conn)
        rows = conn.execute("SELECT * FROM pi_tools ORDER BY builtin DESC, name ASC").fetchall()
        return [_tool_from_row(row) for row in rows]


@router.put("/tools/{tool_id}", response_model=PiTool)
def update_tool(tool_id: str, body: UpdatePiToolRequest):
    now = utcnow()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pi_tools WHERE id = ?", (tool_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Tool not found")
        display_name = body.display_name or row["display_name"] or row["name"]
        conn.execute(
            """
            UPDATE pi_tools
            SET display_name = ?,
                description = ?,
                enabled = ?,
                schema_json = ?,
                config_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                display_name,
                body.description,
                1 if body.enabled else 0,
                json.dumps(body.schema_, ensure_ascii=False, separators=(",", ":")),
                json.dumps(body.config, ensure_ascii=False, separators=(",", ":")),
                now,
                tool_id,
            ),
        )
        updated = conn.execute("SELECT * FROM pi_tools WHERE id = ?", (tool_id,)).fetchone()
        assert updated is not None
        return _tool_from_row(updated)


@router.delete("/tools/{tool_id}", status_code=204)
def delete_tool(tool_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT builtin FROM pi_tools WHERE id = ?", (tool_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Tool not found")
        if bool(row["builtin"]):
            conn.execute(
                "UPDATE pi_tools SET enabled = 0, updated_at = ? WHERE id = ?",
                (utcnow(), tool_id),
            )
            return
        conn.execute("DELETE FROM pi_tools WHERE id = ?", (tool_id,))


def _sync_builtin_tools(conn) -> None:
    now = utcnow()
    for tool in PI_BUILTIN_TOOLS:
        tool_id = f"builtin_{tool['name']}"
        conn.execute(
            """
            INSERT INTO pi_tools (
                id, name, display_name, description, enabled, builtin, source,
                schema_json, config_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 1, 'pi_builtin', ?, '{}', ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                builtin = 1,
                source = 'pi_builtin',
                display_name = excluded.display_name,
                description = excluded.description,
                schema_json = excluded.schema_json,
                updated_at = excluded.updated_at
            """,
            (
                tool_id,
                tool["name"],
                tool["display_name"],
                tool["description"],
                json.dumps(tool["schema"], ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )


def _tool_from_row(row) -> PiTool:
    return PiTool(
        id=row["id"],
        name=row["name"],
        display_name=row["display_name"] or row["name"],
        description=row["description"] or "",
        enabled=bool(row["enabled"]),
        builtin=bool(row["builtin"]),
        source=row["source"] or "custom",
        schema_=_parse_json_object(row["schema_json"]),
        config=_parse_json_object(row["config_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _parse_json_object(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
