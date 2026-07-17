from __future__ import annotations

import json
import os as _os
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from axiom.server.db import get_conn

router = APIRouter(tags=["files"])

_CRAWLER_VIEW_FILENAMES = ("site_map.yaml", "api_inventory.yaml", "page_api_map.yaml")
FILES_ROOT = Path("/tmp/axiom-files")

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}


def _task_dir(project_id: str) -> Path:
    """Get the file directory for a task, creating parent dirs from DB if needed."""
    group_id = "unknown"
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT parent_project_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row and row["parent_project_id"]:
                group_id = str(row["parent_project_id"])
    except Exception:
        pass
    return FILES_ROOT / group_id / project_id


_ALLOWED_FOLDERS = {"human", "ai", "crawler"}


def _ensure_dirs(project_id: str) -> tuple[Path, Path]:
    task_dir = _task_dir(project_id)
    human_dir = task_dir / "human"
    ai_dir = task_dir / "ai"
    human_dir.mkdir(parents=True, exist_ok=True)
    ai_dir.mkdir(parents=True, exist_ok=True)
    return human_dir, ai_dir


def _list_dir_recursive(folder_path: Path) -> list[dict]:
    """List all files recursively, each with relative path."""
    result = []
    if not folder_path.exists():
        return result
    for f in sorted(folder_path.rglob("*"), key=lambda x: str(x.relative_to(folder_path))):
        if f.is_file():
            rel = str(f.relative_to(folder_path))
            result.append({
                "name": f.name,
                "path": rel,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
    return result


@router.get("/files/{project_id}")
def list_files(project_id: str):
    """List files for a task, grouped by human/ai/crawler. Recursive."""
    human_dir, ai_dir = _ensure_dirs(project_id)
    task_dir = _task_dir(project_id)
    crawler_dir = task_dir / "crawler"
    result = {"project_id": project_id, "human": [], "ai": [], "crawler": []}
    for folder, key in [(human_dir, "human"), (ai_dir, "ai"), (crawler_dir, "crawler")]:
        result[key] = _list_dir_recursive(folder)
    return result


@router.get("/files/{project_id}/{folder}/{filename:path}")
def read_file(project_id: str, folder: str, filename: str):
    """Read a file's content. For .md files, returns raw markdown."""
    if folder not in _ALLOWED_FOLDERS:
        raise HTTPException(400, f"folder must be one of {_ALLOWED_FOLDERS}")
    task_dir = _task_dir(project_id)
    file_path = task_dir / folder / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "file not found")
    # Safety: don't allow path traversal
    try:
        file_path.resolve().relative_to(task_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal denied")
    size = file_path.stat().st_size
    max_preview = 5 * 1024 * 1024  # 5MB
    if size > max_preview:
        return {
            "name": filename, "folder": folder,
            "content": f"[文件过大 ({size/1024/1024:.1f}MB)，不支持预览]",
            "size": size,
        }
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = f"[二进制文件 ({size/1024:.1f}KB)，不支持预览]"
    return {
        "name": filename,
        "folder": folder,
        "content": content,
        "size": size,
    }


@router.get("/files-raw/{project_id}/{folder}/{filename:path}")
def read_file_raw(project_id: str, folder: str, filename: str):
    """Serve a file's raw bytes (for images and other binary previews)."""
    if folder not in _ALLOWED_FOLDERS:
        raise HTTPException(400, f"folder must be one of {_ALLOWED_FOLDERS}")
    task_dir = _task_dir(project_id)
    file_path = task_dir / folder / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "file not found")
    try:
        file_path.resolve().relative_to(task_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal denied")
    return FileResponse(file_path)


@router.post("/files/{project_id}/upload")
async def upload_file(project_id: str, file: UploadFile, set_origin: bool = False):
    """Upload a file to the human/ folder. Set set_origin=true to also update the project's origin fact with the full server path."""
    human_dir, _ = _ensure_dirs(project_id)
    if not file.filename:
        raise HTTPException(400, "filename required")
    safe_name = Path(file.filename).name  # strip path components
    dest = human_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)
    if set_origin:
        with get_conn() as conn:
            conn.execute(
                "UPDATE facts SET description = ? WHERE project_id = ? AND id = 'origin'",
                (str(dest), project_id),
            )
    return {"name": safe_name, "folder": "human", "size": len(content), "path": str(dest) if set_origin else None}


@router.delete("/files/{project_id}/{folder}/{filename:path}")
def delete_file(project_id: str, folder: str, filename: str):
    """Delete a file."""
    if folder not in _ALLOWED_FOLDERS:
        raise HTTPException(400, f"folder must be one of {_ALLOWED_FOLDERS}")
    task_dir = _task_dir(project_id)
    file_path = task_dir / folder / filename
    if not file_path.exists():
        raise HTTPException(404, "file not found")
    try:
        file_path.resolve().relative_to(task_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal denied")
    file_path.unlink()
    return {"deleted": True}


# ── Group-level endpoints (for crawler/ etc.) ──

@router.get("/file-groups/{group_id}/{folder}")
def list_group_folder(group_id: str, folder: str):
    """List files in a project-group-level folder (e.g. crawler). Recursive."""
    if folder not in ("crawler",):
        raise HTTPException(400, "invalid group folder")
    group_dir = FILES_ROOT / group_id / folder
    files = _list_dir_recursive(group_dir)
    if folder == "crawler":
        from axiom.server.routers import crawler as crawler_router

        existing = {item["path"] for item in files}
        for name in _CRAWLER_VIEW_FILENAMES:
            if name in existing:
                continue
            if crawler_router._crawler_state_view_exists(group_id, name):
                content = crawler_router._render_crawler_state_snapshot(group_id, name)
                files.append({
                    "name": name,
                    "path": name,
                    "size": len(content.encode("utf-8")),
                    "modified": 0,
                })
    return {"group_id": group_id, "folder": folder, "files": files}


@router.get("/file-groups/{group_id}/{folder}/{filename:path}")
def read_group_file(group_id: str, folder: str, filename: str):
    """Read a file from a project-group-level folder."""
    if folder not in ("crawler",):
        raise HTTPException(400, "invalid group folder")
    group_dir = FILES_ROOT / group_id
    file_path = group_dir / folder / filename
    if not file_path.exists() or not file_path.is_file():
        if folder == "crawler" and filename in _CRAWLER_VIEW_FILENAMES:
            from axiom.server.routers import crawler as crawler_router

            if not crawler_router._crawler_state_view_exists(group_id, filename):
                raise HTTPException(404, "file not found")
            content = crawler_router._render_crawler_state_snapshot(group_id, filename)
            return {
                "name": filename,
                "folder": folder,
                "group_id": group_id,
                "content": content,
                "size": len(content.encode("utf-8")),
            }
        raise HTTPException(404, "file not found")
    try:
        file_path.resolve().relative_to(group_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal denied")
    size = file_path.stat().st_size
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = f"[二进制文件 ({size/1024:.1f}KB)，不支持预览]"
    return {"name": filename, "folder": folder, "group_id": group_id, "content": content, "size": size}


@router.get("/file-groups-raw/{group_id}/{folder}/{filename:path}")
def read_group_file_raw(group_id: str, folder: str, filename: str):
    """Serve a group-level file's raw bytes (for images and binary previews)."""
    if folder not in ("crawler",):
        raise HTTPException(400, "invalid group folder")
    group_dir = FILES_ROOT / group_id
    file_path = group_dir / folder / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "file not found")
    try:
        file_path.resolve().relative_to(group_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal denied")
    return FileResponse(file_path)


@router.delete("/file-groups/{group_id}/{folder}/{filename:path}")
def delete_group_file(group_id: str, folder: str, filename: str):
    """Delete a file from a project-group-level folder."""
    if folder not in ("crawler",):
        raise HTTPException(400, "invalid group folder")
    group_dir = FILES_ROOT / group_id
    file_path = group_dir / folder / filename
    if not file_path.exists():
        raise HTTPException(404, "file not found")
    try:
        file_path.resolve().relative_to(group_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal denied")
    file_path.unlink()
    return {"deleted": True}
