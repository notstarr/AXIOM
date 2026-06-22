"""crawler/router.py — FastAPI routes for the crawler.

This is the public entry point. ``app.py`` imports ``router`` from here,
and ``cli.py`` imports ``run_crawler_supervisor``.
``files.py`` imports ``state_view_exists`` and ``render_state_snapshot``.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import uuid
from typing import Any

import requests
from fastapi import APIRouter, HTTPException

from axiom.server.db import get_conn
from axiom.server.services import utcnow

from .models import (
    CrawlerWorkerConfig,
    CrawlerTestRequest,
    CrawlerTestResponse,
    CrawlerModelsResponse,
    CrawlerStartRequest,
    CrawlerStatus,
    CrawlerTask,
    CreateCrawlerTaskRequest,
)
from .constants import (
    CRAWLER_EXECUTE_PROMPT_PATH,
    CRAWLER_FLUSH_PROMPT_PATH,
    CRAWLER_SESSION_DIR,
)
from .meta import (
    read_meta,
    write_meta,
    is_running,
    load_worker_config,
    get_api_key_from_db_or_body,
    load_crawler_runtime_settings,
)
from .state import (
    load_state_rows,
    state_view_exists,
    render_state_snapshot,
    target_url_for_group,
)
from .cdp import close_crawler_tab
from .supervisor import run_crawler_supervisor

# Re-export for files.py backward compat
_ = render_state_snapshot  # used by files.py

router = APIRouter(tags=["crawler"])


# ── Worker config CRUD ─────────────────────

@router.get("/crawler/worker", response_model=CrawlerWorkerConfig)
def get_crawler_worker():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pi_model, pi_base_url, pi_provider_api, pi_context_window "
            "FROM crawler_workers WHERE id = 1"
        ).fetchone()
    if row is None:
        return CrawlerWorkerConfig()
    return CrawlerWorkerConfig(
        pi_model=row["pi_model"] or "",
        pi_base_url=row["pi_base_url"] or "",
        pi_api_key="",
        pi_provider_api=row["pi_provider_api"] or "openai-completions",
        pi_context_window=row["pi_context_window"] or "128000",
    )


@router.put("/crawler/worker", response_model=CrawlerWorkerConfig)
def update_crawler_worker(body: CrawlerWorkerConfig):
    now = utcnow()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT pi_api_key FROM crawler_workers WHERE id = 1"
        ).fetchone()
        api_key = body.pi_api_key.strip() if body.pi_api_key else (
            existing["pi_api_key"] if existing else ""
        )
        conn.execute(
            """
            UPDATE crawler_workers SET
                pi_model = ?, pi_base_url = ?, pi_api_key = ?,
                pi_provider_api = ?, pi_context_window = ?, updated_at = ?
            WHERE id = 1
            """,
            (body.pi_model, body.pi_base_url, api_key,
             body.pi_provider_api, body.pi_context_window, now),
        )
    return CrawlerWorkerConfig(
        pi_model=body.pi_model,
        pi_base_url=body.pi_base_url,
        pi_api_key="",
        pi_provider_api=body.pi_provider_api,
        pi_context_window=body.pi_context_window,
    )


@router.get("/crawler/worker/models", response_model=CrawlerModelsResponse)
def list_crawler_models(base_url: str = "", api_key: str = ""):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pi_base_url, pi_api_key FROM crawler_workers WHERE id = 1"
        ).fetchone()
    stored_base_url = row["pi_base_url"] if row else ""
    stored_api_key = row["pi_api_key"] if row else ""

    effective_base_url = base_url.strip() or stored_base_url
    effective_api_key = api_key.strip() or stored_api_key

    if not effective_base_url:
        return CrawlerModelsResponse(models=[], error="base_url 未配置")

    url = effective_base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if effective_api_key:
        headers["Authorization"] = f"Bearer {effective_api_key}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as e:
        return CrawlerModelsResponse(models=[], error=f"请求失败: {e}")
    if resp.status_code != 200:
        return CrawlerModelsResponse(models=[], error=f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception:
        return CrawlerModelsResponse(models=[], error="响应不是合法 JSON")

    raw_models: list[dict] = []
    if isinstance(data, dict) and "data" in data:
        raw_models = data["data"] if isinstance(data["data"], list) else []
    elif isinstance(data, list):
        raw_models = data

    def _infer_ctx(model_id: str) -> int:
        return 1000000

    models = []
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id") or m.get("name") or ""
        if not model_id:
            continue
        ctx = (
            m.get("context_length")
            or m.get("context_window")
            or m.get("max_context_length")
            or _infer_ctx(model_id)
        )
        models.append({
            "id": model_id,
            "context_window": int(ctx) if ctx else 128000,
        })

    return CrawlerModelsResponse(models=models)


@router.post("/crawler/worker/test", response_model=CrawlerTestResponse)
def test_crawler_connection(body: CrawlerTestRequest):
    api_key = get_api_key_from_db_or_body(body.pi_api_key)
    if not api_key:
        api_key = ""
    if not body.pi_base_url or not body.pi_model:
        return CrawlerTestResponse(ok=False, message="base_url 或 model 为空")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        if body.pi_provider_api == "openai-responses":
            resp = requests.post(
                body.pi_base_url.rstrip("/") + "/responses",
                headers=headers,
                json={"model": body.pi_model, "input": "ping", "max_output_tokens": 8},
                timeout=15,
            )
        else:
            resp = requests.post(
                body.pi_base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": body.pi_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
                timeout=15,
            )
    except requests.RequestException as e:
        return CrawlerTestResponse(ok=False, message=f"连接失败：{e}")

    if 200 <= resp.status_code < 300:
        return CrawlerTestResponse(ok=True, message="连接成功，模型可用")
    return CrawlerTestResponse(ok=False, message=f"HTTP {resp.status_code}: {resp.text[:300]}")


# ── Per-project crawler endpoints ──────────

@router.post("/project-groups/{group_id}/crawler/start", response_model=CrawlerStatus, status_code=201)
def start_crawler(group_id: str, body: CrawlerStartRequest):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM project_groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Project group {group_id} not found")

    project_id = group_id
    meta = read_meta(project_id)
    if meta.get("pid"):
        pid = meta["pid"]
        if is_running(pid):
            raise HTTPException(409, f"爬虫进程已在运行中 (pid={pid})")

    if not CRAWLER_EXECUTE_PROMPT_PATH.exists():
        raise HTTPException(500, f"CrawlerExecute.md 不存在: {CRAWLER_EXECUTE_PROMPT_PATH}")
    if not CRAWLER_FLUSH_PROMPT_PATH.exists():
        raise HTTPException(500, f"CrawlerFlush.md 不存在: {CRAWLER_FLUSH_PROMPT_PATH}")

    parent_id = group_id
    ai_dir = f"/tmp/axiom-files/{parent_id}/crawler"
    pathlib.Path(ai_dir).mkdir(parents=True, exist_ok=True)

    old_meta = read_meta(project_id)
    if old_meta.get("cdp_tab_id"):
        close_crawler_tab(str(old_meta["cdp_tab_id"]))

    session_id = f"crawler-{project_id}"
    agent_dir = f"{CRAWLER_SESSION_DIR}/{project_id}"
    session_dir = f"{agent_dir}/sessions"
    pathlib.Path(session_dir).mkdir(parents=True, exist_ok=True)

    log_path = f"/tmp/axiom-crawler-{project_id}.log"
    now = utcnow()
    runtime = load_crawler_runtime_settings()
    supervisor_id = f"sup-{project_id}-{uuid.uuid4().hex[:8]}"
    project_root = pathlib.Path(__file__).resolve().parents[4]
    proc = subprocess.Popen(
        [
            "uv", "run", "--project", str(project_root),
            "axiom", "crawler-supervisor", "--project-id", project_id,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    write_meta(project_id, {
        "project_id": project_id,
        "pid": proc.pid,
        "supervisor_id": supervisor_id,
        "phase_pid": None,
        "target_url": body.target_url,
        "output_dir": ai_dir,
        "started_at": now,
        "status": "running",
        "phase": "crawl",
        "round": 1,
        "phase_started_at": now,
        "last_flush_at": None,
        "active_window_seconds": runtime["active_window_seconds"],
        "flush_window_seconds": runtime["flush_window_seconds"],
        "total_timeout_seconds": runtime["total_timeout_seconds"],
        "stop_requested": False,
        "session_id": session_id,
        "cdp_tab_id": "",
        "agent_dir": agent_dir,
        "session_dir": session_dir,
        "log": log_path,
    })

    return CrawlerStatus(
        project_id=project_id,
        status="running",
        phase="crawl",
        round=1,
        target_url=body.target_url,
        output_dir=ai_dir,
        pid=proc.pid,
        supervisor_id=supervisor_id,
        phase_pid=None,
        started_at=now,
        phase_started_at=now,
        last_flush_at=None,
        active_window_seconds=runtime["active_window_seconds"],
        flush_window_seconds=runtime["flush_window_seconds"],
        total_timeout_seconds=runtime["total_timeout_seconds"],
        stop_requested=False,
        sitemap_exists=state_view_exists(project_id, "site_map.yaml"),
        api_list_exists=state_view_exists(project_id, "api_inventory.yaml"),
        api_page_map_exists=state_view_exists(project_id, "page_api_map.yaml"),
    )


@router.get("/project-groups/{group_id}/crawler", response_model=CrawlerStatus)
def get_crawler_status(group_id: str):
    project_id = group_id
    meta = read_meta(project_id)
    if not meta:
        return CrawlerStatus(project_id=project_id, status="idle")

    pid = meta.get("pid")
    supervisor_id = meta.get("supervisor_id")
    phase_pid = meta.get("phase_pid")
    if phase_pid and is_running(phase_pid):
        status = "stopping" if meta.get("stop_requested") else str(meta.get("status") or "running")
    elif pid and is_running(pid):
        status = "stopping" if meta.get("stop_requested") else str(meta.get("status") or "running")
    elif meta.get("status") in {"running", "stopping"}:
        status = "done" if str(meta.get("phase") or "idle") == "idle" and bool(meta.get("phase_accepted")) else "failed"
        meta["status"] = status
        meta["phase"] = "idle"
        meta["pid"] = None
        meta["phase_pid"] = None
        write_meta(project_id, meta)
    else:
        status = str(meta.get("status") or "idle")

    ai_dir = meta.get("output_dir", "")
    return CrawlerStatus(
        project_id=project_id,
        status=status,
        phase=str(meta.get("phase") or "idle"),
        round=int(meta.get("round") or 0),
        target_url=meta.get("target_url", ""),
        output_dir=ai_dir,
        pid=pid if status in {"running", "stopping"} else None,
        supervisor_id=supervisor_id if status in {"running", "stopping", "done", "failed"} else None,
        phase_pid=phase_pid if status in {"running", "stopping"} else None,
        started_at=meta.get("started_at"),
        phase_started_at=meta.get("phase_started_at"),
        last_flush_at=meta.get("last_flush_at"),
        active_window_seconds=int(meta.get("active_window_seconds") or 0),
        flush_window_seconds=int(meta.get("flush_window_seconds") or 0),
        total_timeout_seconds=int(meta.get("total_timeout_seconds") or 0),
        stop_requested=bool(meta.get("stop_requested")),
        sitemap_exists=state_view_exists(project_id, "site_map.yaml"),
        api_list_exists=state_view_exists(project_id, "api_inventory.yaml"),
        api_page_map_exists=state_view_exists(project_id, "page_api_map.yaml"),
    )


@router.post("/project-groups/{group_id}/crawler/stop")
def stop_crawler(group_id: str):
    project_id = group_id
    meta = read_meta(project_id)
    phase_pid = meta.get("phase_pid")
    if not meta.get("status") in {"running", "stopping"}:
        raise HTTPException(404, "没有运行中的爬虫进程")
    if phase_pid and not is_running(phase_pid):
        raise HTTPException(404, f"阶段进程 {phase_pid} 已退出")
    meta["stop_requested"] = True
    meta["status"] = "stopping"
    write_meta(project_id, meta)
    return {"stopped": True, "pid": phase_pid, "stop_requested": True}


@router.get("/project-groups/{group_id}/crawler/results")
def get_crawler_results(group_id: str):
    project_id = group_id
    pages = load_state_rows(project_id, "crawler_pages")
    apis = load_state_rows(project_id, "crawler_apis")
    mappings = load_state_rows(project_id, "crawler_page_maps")
    target = target_url_for_group(project_id)
    return {
        "group_id": project_id,
        "target_url": target,
        "pages": pages,
        "apis": apis,
        "mappings": mappings,
        "counts": {
            "pages": len(pages),
            "apis": len(apis),
            "mappings": len(mappings),
        },
    }


# ── Standalone crawler task endpoints ──────

def _next_crawler_task_id(conn) -> str:
    row = conn.execute("SELECT MAX(CAST(SUBSTR(id, 4) AS INTEGER)) AS m FROM crawler_tasks WHERE id LIKE 'ct_%'").fetchone()
    n = (row["m"] or 0) + 1 if row else 1
    return f"ct_{n:03d}"


def _crawler_task_from_row(row, conn=None) -> CrawlerTask:
    page_count = api_count = mapping_count = 0
    if conn:
        try:
            page_count = conn.execute("SELECT COUNT(*) AS n FROM crawler_pages WHERE group_id = ?", (row["id"],)).fetchone()["n"]
            api_count = conn.execute("SELECT COUNT(*) AS n FROM crawler_apis WHERE group_id = ?", (row["id"],)).fetchone()["n"]
            mapping_count = conn.execute("SELECT COUNT(*) AS n FROM crawler_page_maps WHERE group_id = ?", (row["id"],)).fetchone()["n"]
        except Exception:
            pass
    return CrawlerTask(
        id=row["id"],
        title=row["title"] or "",
        target_url=row["target_url"] or "",
        status=row["status"] or "idle",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        page_count=page_count,
        api_count=api_count,
        mapping_count=mapping_count,
    )


@router.get("/crawler/tasks", response_model=list[CrawlerTask])
def list_crawler_tasks():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM crawler_tasks ORDER BY created_at DESC, id DESC").fetchall()
        return [_crawler_task_from_row(r, conn) for r in rows]


@router.post("/crawler/tasks", response_model=CrawlerTask, status_code=201)
def create_crawler_task(body: CreateCrawlerTaskRequest):
    now = utcnow()
    with get_conn() as conn:
        task_id = _next_crawler_task_id(conn)
        conn.execute(
            "INSERT INTO crawler_tasks (id, title, target_url, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, body.title or task_id, body.target_url, "idle", now, now),
        )
        row = conn.execute("SELECT * FROM crawler_tasks WHERE id = ?", (task_id,)).fetchone()
        return _crawler_task_from_row(row, conn)


@router.delete("/crawler/tasks/{task_id}", status_code=204)
def delete_crawler_task(task_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM crawler_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Crawler task {task_id} not found")
        conn.execute("DELETE FROM crawler_tasks WHERE id = ?", (task_id,))
        conn.execute("DELETE FROM crawler_pages WHERE group_id = ?", (task_id,))
        conn.execute("DELETE FROM crawler_apis WHERE group_id = ?", (task_id,))
        conn.execute("DELETE FROM crawler_page_maps WHERE group_id = ?", (task_id,))
    # Clean up meta file
    mf = pathlib.Path(f"/tmp/axiom-crawler-{task_id}.meta.json")
    if mf.exists():
        mf.unlink(missing_ok=True)


@router.post("/crawler/tasks/{task_id}/start", response_model=CrawlerStatus, status_code=201)
def start_crawler_task(task_id: str, body: CrawlerStartRequest):
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM crawler_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Crawler task {task_id} not found")

    project_id = task_id
    meta = read_meta(project_id)
    if meta.get("pid"):
        pid = meta["pid"]
        if is_running(pid):
            raise HTTPException(409, f"爬虫进程已在运行中 (pid={pid})")

    if not CRAWLER_EXECUTE_PROMPT_PATH.exists():
        raise HTTPException(500, f"CrawlerExecute.md 不存在: {CRAWLER_EXECUTE_PROMPT_PATH}")
    if not CRAWLER_FLUSH_PROMPT_PATH.exists():
        raise HTTPException(500, f"CrawlerFlush.md 不存在: {CRAWLER_FLUSH_PROMPT_PATH}")

    ai_dir = f"/tmp/axiom-files/{task_id}/crawler"
    pathlib.Path(ai_dir).mkdir(parents=True, exist_ok=True)

    old_meta = read_meta(project_id)
    if old_meta.get("cdp_tab_id"):
        close_crawler_tab(str(old_meta["cdp_tab_id"]))

    session_id = f"crawler-{project_id}"
    agent_dir = f"{CRAWLER_SESSION_DIR}/{project_id}"
    session_dir = f"{agent_dir}/sessions"
    pathlib.Path(session_dir).mkdir(parents=True, exist_ok=True)

    log_path = f"/tmp/axiom-crawler-{project_id}.log"
    now = utcnow()
    runtime = load_crawler_runtime_settings()
    supervisor_id = f"sup-{project_id}-{uuid.uuid4().hex[:8]}"
    project_root = pathlib.Path(__file__).resolve().parents[4]
    proc = subprocess.Popen(
        [
            "uv", "run", "--project", str(project_root),
            "axiom", "crawler-supervisor", "--project-id", project_id,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    write_meta(project_id, {
        "project_id": project_id,
        "pid": proc.pid,
        "supervisor_id": supervisor_id,
        "phase_pid": None,
        "target_url": body.target_url,
        "output_dir": ai_dir,
        "started_at": now,
        "status": "running",
        "phase": "crawl",
        "round": 1,
        "phase_started_at": now,
        "last_flush_at": None,
        "active_window_seconds": runtime["active_window_seconds"],
        "flush_window_seconds": runtime["flush_window_seconds"],
        "total_timeout_seconds": runtime["total_timeout_seconds"],
        "stop_requested": False,
        "session_id": session_id,
        "cdp_tab_id": "",
        "agent_dir": agent_dir,
        "session_dir": session_dir,
        "log": log_path,
    })

    with get_conn() as conn:
        conn.execute("UPDATE crawler_tasks SET status = 'running', target_url = ?, updated_at = ? WHERE id = ?", (body.target_url, now, task_id))

    return CrawlerStatus(
        project_id=project_id,
        status="running",
        phase="crawl",
        round=1,
        target_url=body.target_url,
        output_dir=ai_dir,
        pid=proc.pid,
        supervisor_id=supervisor_id,
        phase_pid=None,
        started_at=now,
        phase_started_at=now,
        last_flush_at=None,
        active_window_seconds=runtime["active_window_seconds"],
        flush_window_seconds=runtime["flush_window_seconds"],
        total_timeout_seconds=runtime["total_timeout_seconds"],
        stop_requested=False,
        sitemap_exists=state_view_exists(project_id, "site_map.yaml"),
        api_list_exists=state_view_exists(project_id, "api_inventory.yaml"),
        api_page_map_exists=state_view_exists(project_id, "page_api_map.yaml"),
    )


@router.get("/crawler/tasks/{task_id}/status", response_model=CrawlerStatus)
def get_crawler_task_status(task_id: str):
    project_id = task_id
    meta = read_meta(project_id)
    if not meta:
        return CrawlerStatus(project_id=project_id, status="idle")

    pid = meta.get("pid")
    phase_pid = meta.get("phase_pid")
    if phase_pid and is_running(phase_pid):
        status = "stopping" if meta.get("stop_requested") else str(meta.get("status") or "running")
    elif pid and is_running(pid):
        status = "stopping" if meta.get("stop_requested") else str(meta.get("status") or "running")
    elif meta.get("status") in {"running", "stopping"}:
        status = "done" if str(meta.get("phase") or "idle") == "idle" and bool(meta.get("phase_accepted")) else "failed"
        meta["status"] = status
        meta["phase"] = "idle"
        meta["pid"] = None
        meta["phase_pid"] = None
        write_meta(project_id, meta)
        with get_conn() as conn:
            conn.execute("UPDATE crawler_tasks SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), task_id))
    else:
        status = str(meta.get("status") or "idle")

    return CrawlerStatus(
        project_id=project_id,
        status=status,
        phase=str(meta.get("phase") or "idle"),
        round=int(meta.get("round") or 0),
        target_url=meta.get("target_url", ""),
        output_dir=meta.get("output_dir", ""),
        pid=pid if status in {"running", "stopping"} else None,
        supervisor_id=meta.get("supervisor_id") if status in {"running", "stopping", "done", "failed"} else None,
        phase_pid=phase_pid if status in {"running", "stopping"} else None,
        started_at=meta.get("started_at"),
        phase_started_at=meta.get("phase_started_at"),
        last_flush_at=meta.get("last_flush_at"),
        active_window_seconds=int(meta.get("active_window_seconds") or 0),
        flush_window_seconds=int(meta.get("flush_window_seconds") or 0),
        total_timeout_seconds=int(meta.get("total_timeout_seconds") or 0),
        stop_requested=bool(meta.get("stop_requested")),
        sitemap_exists=state_view_exists(project_id, "site_map.yaml"),
        api_list_exists=state_view_exists(project_id, "api_inventory.yaml"),
        api_page_map_exists=state_view_exists(project_id, "page_api_map.yaml"),
    )


@router.post("/crawler/tasks/{task_id}/stop")
def stop_crawler_task(task_id: str):
    project_id = task_id
    meta = read_meta(project_id)
    if not meta.get("status") in {"running", "stopping"}:
        raise HTTPException(404, "没有运行中的爬虫进程")
    meta["stop_requested"] = True
    meta["status"] = "stopping"
    write_meta(project_id, meta)
    return {"stopped": True, "stop_requested": True}


@router.get("/crawler/tasks/{task_id}/results")
def get_crawler_task_results(task_id: str):
    pages = load_state_rows(task_id, "crawler_pages")
    apis = load_state_rows(task_id, "crawler_apis")
    mappings = load_state_rows(task_id, "crawler_page_maps")
    target = target_url_for_group(task_id)
    return {
        "group_id": task_id,
        "target_url": target,
        "pages": pages,
        "apis": apis,
        "mappings": mappings,
        "counts": {"pages": len(pages), "apis": len(apis), "mappings": len(mappings)},
    }


@router.get("/crawler/tasks/{task_id}/export")
def export_crawler_task_yaml(task_id: str):
    """Export all three YAML files as a single combined text for plan input."""
    parts = []
    for filename in ("site_map.yaml", "api_inventory.yaml", "page_api_map.yaml"):
        if state_view_exists(task_id, filename):
            snapshot = render_state_snapshot(task_id, filename)
            parts.append(f"### {filename}\n{snapshot}")
    if not parts:
        raise HTTPException(404, "No crawler data available")
    return {"yaml": "\n\n".join(parts)}
