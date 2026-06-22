from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from axiom.server.db import get_conn
from axiom.server.models import (
    CreatePiToolRequest,
    PiTool,
    UpdatePiToolRequest,
)
from axiom.server.services import utcnow

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


# ── Skills (file-based, no DB) ──

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "dispatcher" / "skills"


def _parse_frontmatter(raw: str) -> dict:
    """Parse YAML frontmatter from SKILL.md content."""
    info = {"name": "", "description": "", "disabled": False}
    text = raw.strip()
    if not text.startswith("---"):
        return info
    second = text.find("---", 3)
    if second == -1:
        return info
    fm = text[3:second].strip()
    for line in fm.split("\n"):
        line = line.strip()
        if line.startswith("name:"):
            info["name"] = line[5:].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            info["description"] = line[12:].strip().strip('"').strip("'")
        elif line.startswith("disable-model-invocation:"):
            info["disabled"] = line[26:].strip().lower() in ("true", "yes", "1")
    return info


def _skill_info(skill_dir: pathlib.Path) -> dict | None:
    """Read skill info from a skill directory."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return None
    raw = skill_file.read_text(encoding="utf-8")
    fm = _parse_frontmatter(raw)
    stat = skill_file.stat()
    return {
        "id": skill_dir.name,
        "name": fm["name"] or skill_dir.name,
        "display_name": skill_dir.name,
        "description": fm["description"],
        "content": raw,
        "enabled": not fm["disabled"],
        "builtin": True,
        "source": "builtin",
        "created_at": "",
        "updated_at": "",
    }


@router.get("/skills")
def list_skills():
    """List all skills by scanning the skills directory."""
    if not _SKILLS_DIR.is_dir():
        return []
    skills = []
    for entry in sorted(_SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        info = _skill_info(entry)
        if info:
            skills.append(info)
    return skills


class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""
    content: str = ""


@router.post("/skills", status_code=201)
def create_skill(body: CreateSkillRequest):
    name = body.name.strip().lower()
    if not name or not all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        raise HTTPException(400, "Name must be lowercase a-z, 0-9, hyphens only")
    skill_dir = _SKILLS_DIR / name
    if skill_dir.exists():
        raise HTTPException(409, f"Skill '{name}' already exists")
    skill_dir.mkdir(parents=True)
    content = body.content or f"---\nname: {name}\ndescription: {body.description}\n---\n\n"
    if not content.startswith("---"):
        content = f"---\nname: {name}\ndescription: {body.description}\n---\n\n{content}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return _skill_info(skill_dir)


class UpdateSkillRequest(BaseModel):
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None


@router.put("/skills/{skill_name}")
def update_skill(skill_name: str, body: UpdateSkillRequest):
    skill_dir = _SKILLS_DIR / skill_name
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    raw = skill_file.read_text(encoding="utf-8")
    fm = _parse_frontmatter(raw)

    # Determine new content
    new_content = body.content if body.content is not None else raw
    new_desc = body.description if body.description is not None else fm["description"]
    new_disabled = not body.enabled if body.enabled is not None else fm["disabled"]

    # Reassemble with frontmatter
    if new_content.startswith("---"):
        # Replace frontmatter in existing content
        second = new_content.find("---", 3)
        if second != -1:
            body_text = new_content[second + 3:].strip()
        else:
            body_text = new_content
    else:
        body_text = new_content.strip()

    disable_line = "\ndisable-model-invocation: true" if new_disabled else ""
    full = f"---\nname: {fm['name'] or skill_name}\ndescription: {new_desc}{disable_line}\n---\n\n{body_text}"
    skill_file.write_text(full, encoding="utf-8")
    return _skill_info(skill_dir)


@router.delete("/skills/{skill_name}", status_code=204)
def delete_skill(skill_name: str):
    skill_dir = _SKILLS_DIR / skill_name
    if not skill_dir.is_dir():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    import shutil
    shutil.rmtree(skill_dir)


# ── Skill file management (multi-file skills) ──

def _skill_dir_path(name: str) -> pathlib.Path:
    return _SKILLS_DIR / name


def _validate_rel_path(path: str) -> str:
    if not path or ".." in path.split("/"):
        raise HTTPException(400, "Invalid path")
    if path.startswith("/"):
        raise HTTPException(400, "Path must be relative")
    return path


@router.get("/skills/{skill_name}/files")
def list_skill_files(skill_name: str) -> list[dict[str, Any]]:
    sd = _skill_dir_path(skill_name)
    if not sd.is_dir():
        return []
    files = []
    for root, _dirs, fnames in os.walk(sd):
        for fname in sorted(fnames):
            full = pathlib.Path(root) / fname
            rel = str(full.relative_to(sd))
            files.append({"path": rel, "size": full.stat().st_size})
    return sorted(files, key=lambda f: f["path"])


@router.get("/skills/{skill_name}/files/{file_path:path}")
def read_skill_file(skill_name: str, file_path: str):
    _validate_rel_path(file_path)
    sd = _skill_dir_path(skill_name)
    full = sd / file_path
    if not full.is_file():
        raise HTTPException(404, f"File not found: {file_path}")
    return {"path": file_path, "content": full.read_text(encoding="utf-8")}


class WriteSkillFileRequest(BaseModel):
    content: str
    path: str = ""


@router.put("/skills/{skill_name}/files/{file_path:path}")
def write_skill_file(skill_name: str, file_path: str, body: WriteSkillFileRequest):
    target_path = body.path or file_path
    _validate_rel_path(target_path)
    sd = _skill_dir_path(skill_name)
    if not sd.is_dir():
        raise HTTPException(404, f"Skill directory not found: {skill_name}")
    full = sd / target_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body.content, encoding="utf-8")
    return {"path": target_path, "saved": True}


@router.delete("/skills/{skill_name}/files/{file_path:path}", status_code=204)
def delete_skill_file(skill_name: str, file_path: str):
    _validate_rel_path(file_path)
    if file_path == "SKILL.md":
        raise HTTPException(400, "Cannot delete SKILL.md")
    sd = _skill_dir_path(skill_name)
    full = sd / file_path
    if full.is_file():
        full.unlink()
    parent = full.parent
    while parent != sd and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


class CreateSkillFileRequest(BaseModel):
    path: str
    content: str = ""


@router.post("/skills/{skill_name}/files", status_code=201)
def create_skill_file(skill_name: str, body: CreateSkillFileRequest):
    _validate_rel_path(body.path)
    if body.path == "SKILL.md":
        raise HTTPException(400, "SKILL.md already exists")
    sd = _skill_dir_path(skill_name)
    if not sd.is_dir():
        raise HTTPException(404, f"Skill directory not found: {skill_name}")
    full = sd / body.path
    if full.exists():
        raise HTTPException(409, f"File already exists: {body.path}")
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body.content, encoding="utf-8")
    return {"path": body.path, "created": True}
