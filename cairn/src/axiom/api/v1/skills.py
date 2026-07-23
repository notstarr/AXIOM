from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from axiom.api.v1.dependencies import get_v2_engine
from axiom.infrastructure.database.metadata import audit_events


router = APIRouter(prefix="/skills", tags=["v1-skills"])
SKILLS_DIR = Path(__file__).resolve().parents[2] / "dispatcher" / "skills"


class CreateSkill(BaseModel):
    name: str
    description: str = ""
    content: str = ""
    enabled: bool = True


class UpdateSkill(BaseModel):
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None


class WriteSkillFile(BaseModel):
    content: str = ""
    path: str = ""


def _frontmatter(raw: str) -> dict[str, object]:
    info: dict[str, object] = {"name": "", "description": "", "disabled": False}
    match = re.match(r"^---\s*\n(.*?)\n---", raw, re.S)
    if not match:
        return info
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in {"name", "description"}:
            info[key] = value
        elif key == "disable-model-invocation":
            info["disabled"] = value.lower() in {"true", "yes", "1"}
    return info


def _skill_info(skill_dir: Path) -> dict | None:
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        return None
    raw = skill_file.read_text(encoding="utf-8")
    info = _frontmatter(raw)
    stat = skill_file.stat()
    return {
        "id": skill_dir.name,
        "name": str(info["name"] or skill_dir.name),
        "display_name": skill_dir.name,
        "description": str(info["description"] or ""),
        "content": raw,
        "enabled": not bool(info["disabled"]),
        "builtin": True,
        "source": "dispatcher",
        "created_at": "",
        "updated_at": str(stat.st_mtime_ns),
    }


def _skill_dir(name: str) -> Path:
    normalized = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+", normalized):
        raise HTTPException(400, "Name must be lowercase a-z, 0-9, hyphens only")
    return SKILLS_DIR / normalized


def _file_path(skill_name: str, relative: str) -> Path:
    relative = relative.replace("\\", "/").strip("/")
    if not relative or ".." in relative.split("/"):
        raise HTTPException(400, "Invalid path")
    root = _skill_dir(skill_name).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "Invalid path") from exc
    return path


def _render_skill(name: str, description: str, body: str, *, enabled: bool) -> str:
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", body, re.S)
    if match:
        body = body[match.end() :]
    disabled = "\ndisable-model-invocation: true" if not enabled else ""
    return f"---\nname: {name}\ndescription: {description}{disabled}\n---\n\n{body.strip()}\n"


@router.get("")
def list_skills():
    if not SKILLS_DIR.is_dir():
        return []
    return [
        info
        for entry in sorted(SKILLS_DIR.iterdir())
        if entry.is_dir() and (info := _skill_info(entry)) is not None
    ]


@router.get("/usage")
def skill_usage(engine=Depends(get_v2_engine)):
    pattern = re.compile(r"(?:^|/)skills/([^/]+)/SKILL\.md(?:$|[\s\"'])")
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.payload_json).where(
                audit_events.c.payload_json.ilike("%/skills/%/SKILL.md%")
            )
        )
        for row in rows:
            names = set(pattern.findall(str(row.payload_json or "")))
            for name in names:
                counts[name] = counts.get(name, 0) + 1
    return {
        "items": [
            {"skill": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]
    }


@router.post("", status_code=201)
def create_skill(body: CreateSkill):
    skill_dir = _skill_dir(body.name)
    if skill_dir.exists():
        raise HTTPException(409, f"Skill '{body.name}' already exists")
    skill_dir.mkdir(parents=True)
    content = _render_skill(
        skill_dir.name,
        body.description.strip(),
        body.content,
        enabled=body.enabled,
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return _skill_info(skill_dir)


@router.put("/{skill_name}")
def update_skill(skill_name: str, body: UpdateSkill):
    skill_dir = _skill_dir(skill_name)
    current = _skill_info(skill_dir)
    if current is None:
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    content = body.content if body.content is not None else str(current["content"])
    description = body.description if body.description is not None else str(current["description"])
    enabled = body.enabled if body.enabled is not None else bool(current["enabled"])
    (skill_dir / "SKILL.md").write_text(
        _render_skill(skill_dir.name, description.strip(), content, enabled=enabled),
        encoding="utf-8",
    )
    return _skill_info(skill_dir)


@router.delete("/{skill_name}", status_code=204)
def delete_skill(skill_name: str) -> None:
    skill_dir = _skill_dir(skill_name)
    if not skill_dir.is_dir():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    shutil.rmtree(skill_dir)


@router.get("/{skill_name}/files")
def list_skill_files(skill_name: str):
    skill_dir = _skill_dir(skill_name)
    if not skill_dir.is_dir():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    files = []
    for root, _dirs, names in os.walk(skill_dir):
        for name in names:
            path = Path(root) / name
            files.append({"path": str(path.relative_to(skill_dir)), "size": path.stat().st_size})
    return sorted(files, key=lambda item: item["path"])


@router.get("/{skill_name}/files/{file_path:path}")
def read_skill_file(skill_name: str, file_path: str):
    path = _file_path(skill_name, file_path)
    if not path.is_file():
        raise HTTPException(404, f"File not found: {file_path}")
    return {"path": file_path, "content": path.read_text(encoding="utf-8")}


@router.put("/{skill_name}/files/{file_path:path}")
def write_skill_file(skill_name: str, file_path: str, body: WriteSkillFile):
    if not _skill_dir(skill_name).is_dir():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    path = _file_path(skill_name, body.path or file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return {"path": str(path.relative_to(_skill_dir(skill_name))), "saved": True}


@router.post("/{skill_name}/files", status_code=201)
def create_skill_file(skill_name: str, body: WriteSkillFile):
    path = _file_path(skill_name, body.path)
    if path.exists():
        raise HTTPException(409, f"File already exists: {body.path}")
    if not _skill_dir(skill_name).is_dir():
        raise HTTPException(404, f"Skill '{skill_name}' not found")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return {"path": body.path, "created": True}


@router.delete("/{skill_name}/files/{file_path:path}", status_code=204)
def delete_skill_file(skill_name: str, file_path: str) -> None:
    if file_path == "SKILL.md":
        raise HTTPException(400, "Cannot delete SKILL.md")
    path = _file_path(skill_name, file_path)
    if path.is_file():
        path.unlink()
