"""
crawler.py — 爬虫 Router

完全独立于 dispatcher 调度循环。
直接 spawn pi 进程执行 other/Crawler.md 提示词，不创建任何 intent。

API:
    GET  /crawler/worker               — 读取爬虫 worker 配置
    PUT  /crawler/worker               — 保存爬虫 worker 配置
    GET  /crawler/worker/models        — 从 base_url 获取可用模型列表
    POST /crawler/worker/test          — 测试连接
    POST /projects/{pid}/crawler/start  — 对某个任务启动爬虫
    GET  /projects/{pid}/crawler        — 查询爬虫运行状态
    POST /projects/{pid}/crawler/stop   — 停止爬虫进程
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import time
import uuid
from urllib.parse import urlsplit
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import yaml

from axiom.server.db import current_db_path, get_conn
from axiom.server.services import get_project_or_404, utcnow

router = APIRouter(tags=["crawler"])

CRAWLER_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "dispatcher" / "prompts" / "other" / "Crawler.md"
)
CRAWLER_EXECUTE_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "dispatcher" / "prompts" / "other" / "CrawlerExecute.md"
)
CRAWLER_FLUSH_PROMPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "dispatcher" / "prompts" / "other" / "CrawlerFlush.md"
)
CRAWLER_SESSION_DIR = "/tmp/axiom-crawler"
_CRAWL_TOOLS = "bash,read_file,write_file,list_dir"
_FLUSH_TOOLS = "bash,read_file,write_file,list_dir"


# ─────────────────────────────────────────
# Models
# ─────────────────────────────────────────

class CrawlerWorkerConfig(BaseModel):
    pi_model: str = ""
    pi_base_url: str = ""
    pi_api_key: str = ""
    pi_provider_api: str = "openai-completions"
    pi_context_window: str = "128000"


class CrawlerTestRequest(BaseModel):
    pi_base_url: str
    pi_model: str
    pi_api_key: str = ""
    pi_provider_api: str = "openai-completions"


class CrawlerTestResponse(BaseModel):
    ok: bool
    message: str


class CrawlerModelsResponse(BaseModel):
    models: list[dict[str, Any]]
    error: str = ""


class CrawlerStartRequest(BaseModel):
    target_url: str

    def model_post_init(self, _ctx):
        self.target_url = self.target_url.strip()
        if not self.target_url:
            raise ValueError("target_url must not be empty")


class CrawlerStatus(BaseModel):
    project_id: str
    status: str = "idle"          # idle | running | stopping | done | failed
    phase: str = "idle"           # idle | crawl | flush | final_flush
    round: int = 0
    target_url: str = ""
    output_dir: str = ""
    pid: int | None = None
    supervisor_id: str | None = None
    phase_pid: int | None = None
    started_at: str | None = None
    phase_started_at: str | None = None
    last_flush_at: str | None = None
    active_window_seconds: int = 0
    flush_window_seconds: int = 0
    total_timeout_seconds: int = 0
    stop_requested: bool = False
    sitemap_exists: bool = False
    api_list_exists: bool = False
    api_page_map_exists: bool = False


# ─────────────────────────────────────────
# Worker config CRUD
# ─────────────────────────────────────────

@router.get("/crawler/worker", response_model=CrawlerWorkerConfig)
def get_crawler_worker():
    """Return the current crawler worker configuration."""
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
        pi_api_key="",          # never expose stored key
        pi_provider_api=row["pi_provider_api"] or "openai-completions",
        pi_context_window=row["pi_context_window"] or "128000",
    )


@router.put("/crawler/worker", response_model=CrawlerWorkerConfig)
def update_crawler_worker(body: CrawlerWorkerConfig):
    """Save crawler worker configuration. Pass empty pi_api_key to keep existing key."""
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


def _get_api_key_from_db_or_body(api_key_body: str) -> str:
    """Use provided key, or fall back to stored key."""
    if api_key_body.strip():
        return api_key_body.strip()
    with get_conn() as conn:
        row = conn.execute("SELECT pi_api_key FROM crawler_workers WHERE id = 1").fetchone()
    return (row["pi_api_key"] or "") if row else ""


@router.get("/crawler/worker/models", response_model=CrawlerModelsResponse)
def list_crawler_models(base_url: str = "", api_key: str = ""):
    """Fetch available models from the configured base_url GET /models endpoint."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pi_base_url, pi_api_key FROM crawler_workers WHERE id = 1"
        ).fetchone()
    stored_base_url = row["pi_base_url"] if row else ""
    stored_api_key = row["pi_api_key"] if row else ""

    # Use provided values, fall back to stored values
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

    # OpenAI-compatible: {"data": [{"id": "...", ...}]}
    raw_models: list[dict] = []
    if isinstance(data, dict) and "data" in data:
        raw_models = data["data"] if isinstance(data["data"], list) else []
    elif isinstance(data, list):
        raw_models = data

    # Infer context window from known model name patterns
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
    """Send a minimal chat completion to verify the model is reachable."""
    api_key = _get_api_key_from_db_or_body(body.pi_api_key)
    if not api_key:
        # Last resort: try without key (some endpoints don't require auth)
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


# ─────────────────────────────────────────
# Per-project crawler endpoints
# ─────────────────────────────────────────

def _pid_file(project_id: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/axiom-crawler-{project_id}.pid")


def _meta_file(project_id: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/axiom-crawler-{project_id}.meta.json")


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        stat = pathlib.Path(f"/proc/{pid}/stat")
        if stat.exists():
            parts = stat.read_text().split()
            if len(parts) >= 3 and parts[2] == "Z":
                return False
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip().startswith("Z"):
                return False
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _read_meta(project_id: str) -> dict:
    f = _meta_file(project_id)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _write_meta(project_id: str, data: dict) -> None:
    _meta_file(project_id).write_text(json.dumps(data, ensure_ascii=False))


def _load_worker_config() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pi_model, pi_base_url, pi_api_key, pi_provider_api, pi_context_window "
            "FROM crawler_workers WHERE id = 1"
        ).fetchone()
    if row is None:
        raise HTTPException(400, "爬虫 worker 尚未配置，请先在系统设置中配置")
    if not row["pi_model"] or not row["pi_base_url"]:
        raise HTTPException(400, "爬虫 worker 配置不完整（缺少 pi_model 或 pi_base_url）")
    return dict(row)


def _schema_output_exists(base: pathlib.Path, legacy_name: str, schema_v1_name: str) -> bool:
    return (base / schema_v1_name).exists() or (base / legacy_name).exists()


def _load_yaml_file(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _merge_unique_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in existing}
    merged = list(existing)
    for item in incoming:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _upsert_crawler_state_row(conn, table: str, group_id: str, row_id: str, payload: dict[str, Any]) -> None:
    existing = conn.execute(
        f"SELECT data_json FROM {table} WHERE group_id = ? AND {table[:-1]}_id = ?",
        (group_id, row_id),
    ).fetchone()
    merged = payload
    if existing:
        try:
            current = json.loads(existing["data_json"] or "{}")
        except json.JSONDecodeError:
            current = {}
        if isinstance(current, dict):
            merged = dict(current)
            for key, value in payload.items():
                if isinstance(value, list) and isinstance(merged.get(key), list):
                    merged[key] = _merge_unique_list(merged[key], value)
                elif value not in (None, "", [], {}):
                    merged[key] = value
    conn.execute(
        f"""
        INSERT INTO {table} (group_id, {table[:-1]}_id, data_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id, {table[:-1]}_id) DO UPDATE SET
            data_json = excluded.data_json,
            updated_at = excluded.updated_at
        """,
        (group_id, row_id, json.dumps(merged, ensure_ascii=False, separators=(",", ":")), utcnow()),
    )


def _persist_schema_files_into_state(group_id: str, ai_dir: str) -> None:
    base = pathlib.Path(ai_dir)
    site_map = _load_yaml_file(base / "site_map.yaml")
    api_inventory = _load_yaml_file(base / "api_inventory.yaml")
    page_api_map = _load_yaml_file(base / "page_api_map.yaml")
    with get_conn() as conn:
        for page in site_map.get("pages", []) or []:
            if isinstance(page, dict) and page.get("id"):
                _upsert_crawler_state_row(conn, "crawler_pages", group_id, str(page["id"]), page)
        for api in api_inventory.get("apis", []) or []:
            if isinstance(api, dict) and api.get("id"):
                _upsert_crawler_state_row(conn, "crawler_apis", group_id, str(api["id"]), api)
        for mapping in page_api_map.get("mappings", []) or []:
            if isinstance(mapping, dict) and mapping.get("id"):
                _upsert_crawler_state_row(conn, "crawler_page_maps", group_id, str(mapping["id"]), mapping)


def _export_state_to_schema_files(group_id: str, ai_dir: str, target_url: str) -> None:
    base = pathlib.Path(ai_dir)
    base.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        pages = [json.loads(row["data_json"]) for row in conn.execute(
            "SELECT data_json FROM crawler_pages WHERE group_id = ? ORDER BY updated_at, page_id", (group_id,)
        ).fetchall()]
        apis = [json.loads(row["data_json"]) for row in conn.execute(
            "SELECT data_json FROM crawler_apis WHERE group_id = ? ORDER BY updated_at, api_id", (group_id,)
        ).fetchall()]
        mappings = [json.loads(row["data_json"]) for row in conn.execute(
            "SELECT data_json FROM crawler_page_maps WHERE group_id = ? ORDER BY updated_at, map_id", (group_id,)
        ).fetchall()]
    target = {
        "url": target_url,
        "domain": _normalize_target_domain(target_url),
        "started_at": utcnow(),
    }
    (base / "site_map.yaml").write_text(yaml.safe_dump({"version": 1, "target": target, "pages": pages}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (base / "api_inventory.yaml").write_text(yaml.safe_dump({"version": 1, "target": target, "apis": apis}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (base / "page_api_map.yaml").write_text(yaml.safe_dump({"version": 1, "target": target, "mappings": mappings}, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _log_contains_final_summary(log_path: str) -> bool:
    path = pathlib.Path(log_path)
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return '"accepted": true' in text and "description" in text


def _normalize_target_domain(target_url: str) -> str:
    return urlsplit(target_url.strip()).hostname or ""


def _load_crawler_runtime_settings() -> dict[str, int]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT crawler_active_window_seconds, crawler_flush_window_seconds, crawler_total_timeout_seconds "
            "FROM settings WHERE rowid = 1"
        ).fetchone()
    return {
        "active_window_seconds": int(row["crawler_active_window_seconds"]),
        "flush_window_seconds": int(row["crawler_flush_window_seconds"]),
        "total_timeout_seconds": int(row["crawler_total_timeout_seconds"]),
    }


def _phase_deadline_seconds(meta: dict) -> int:
    phase = str(meta.get("phase") or "idle")
    if phase in {"flush", "final_flush"}:
        return int(meta.get("flush_window_seconds") or 0)
    if phase == "crawl":
        return int(meta.get("active_window_seconds") or 0)
    return 0


def _seconds_until_phase_switch(meta: dict) -> int | None:
    started_at = meta.get("phase_started_at")
    if not started_at:
        return None
    try:
        from datetime import datetime, timezone
        started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    budget = _phase_deadline_seconds(meta)
    if budget <= 0:
        return None
    remaining = budget - int(time.time() - started.timestamp())
    return max(0, remaining)


def _build_prompt(template_path: pathlib.Path, *, session_id: str, target_url: str, ai_dir: str) -> str:
    prompt_template = template_path.read_text(encoding="utf-8")
    intent_description = f"爬取目标站点 {target_url}，产出站点地图、API 清单和页面-API 映射到 {ai_dir}"
    return (
        prompt_template
        .replace("{intent_id}", session_id)
        .replace("{intent_description}", intent_description)
        .replace("{ai_dir}", ai_dir)
        .replace("<TARGET_DOMAIN>", _normalize_target_domain(target_url))
        .replace("<TARGET_URL>", target_url)
        .replace("<NOW_UTC>", utcnow())
    )


def _build_pi_argv(*, cfg: dict, agent_dir: str, session_dir: str, prompt: str, tools: str) -> list[str]:
    models_json = _build_models_json(cfg)
    wrap_script = (
        'agent_dir="$1"\n'
        'models_json="$2"\n'
        "shift 2\n"
        'mkdir -p "$agent_dir"\n'
        'mkdir -p "$agent_dir/sessions"\n'
        'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
        'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
    )
    pi_args = [
        "--no-extensions",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--tools", tools,
        "--provider", "axiom",
        "--model", cfg["pi_model"],
        "--session-dir", session_dir,
        "-p", prompt,
    ]
    return ["/bin/sh", "-lc", wrap_script, "--", agent_dir, models_json, *pi_args]


def _run_phase_once(*, cfg: dict, meta: dict, prompt_path: pathlib.Path, phase: str) -> None:
    ai_dir = str(meta["output_dir"])
    session_id = str(meta["session_id"])
    agent_dir = str(meta["agent_dir"])
    session_dir = str(meta["session_dir"])
    prompt = _build_prompt(
        prompt_path,
        session_id=session_id,
        target_url=str(meta["target_url"]),
        ai_dir=ai_dir,
    )
    tools = _FLUSH_TOOLS if phase in {"flush", "final_flush"} else _CRAWL_TOOLS
    argv = _build_pi_argv(cfg=cfg, agent_dir=agent_dir, session_dir=session_dir, prompt=prompt, tools=tools)
    log_path = str(meta["log"])
    phase_started_at = utcnow()
    meta["phase"] = phase
    meta["status"] = "stopping" if meta.get("stop_requested") else "running"
    meta["phase_started_at"] = phase_started_at
    _write_meta(str(meta["project_id"]), meta)

    with open(log_path, "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== phase {phase} start {phase_started_at} ===\n")
        log_f.flush()
        if phase in {"flush", "final_flush"}:
            # Flush should summarize using files and recent evidence, not keep a
            # conflicting browser-use session alive from the crawl phase.
            subprocess.run(
                [
                    "/bin/bash",
                    "-lc",
                    f"source ~/browser-use-env/bin/activate && browser-use --session {session_id} close >/dev/null 2>&1 || true",
                ],
                check=False,
            )
        proc = subprocess.Popen(
            argv,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
        meta["phase_pid"] = proc.pid
        _write_meta(str(meta["project_id"]), meta)
        timeout = int(meta["flush_window_seconds"] if phase in {"flush", "final_flush"} else meta["active_window_seconds"])
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
        finally:
            meta["phase_pid"] = None
            if phase in {"flush", "final_flush"}:
                _persist_schema_files_into_state(str(meta["project_id"]), str(meta["output_dir"]))
                _export_state_to_schema_files(str(meta["project_id"]), str(meta["output_dir"]), str(meta["target_url"]))
                meta["last_flush_at"] = utcnow()
                base = pathlib.Path(str(meta["output_dir"]))
                if not (
                    _schema_output_exists(base, "sitemap.yaml", "site_map.yaml")
                    and _schema_output_exists(base, "api-list.yaml", "api_inventory.yaml")
                    and _schema_output_exists(base, "api-page-map.yaml", "page_api_map.yaml")
                ):
                    raise RuntimeError(f"{phase} phase did not persist required schema files")
            _write_meta(str(meta["project_id"]), meta)


def _run_crawler_supervisor(project_id: str) -> None:
    meta = _read_meta(project_id)
    if not meta:
        return
    with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== supervisor start {utcnow()} project={project_id} ===\n")
    cfg = _load_worker_config()
    started_at = meta.get("started_at")
    total_timeout = int(meta.get("total_timeout_seconds") or 0)
    total_deadline = time.time() + total_timeout
    if started_at:
        try:
            from datetime import datetime, timezone
            started = datetime.strptime(str(started_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            total_deadline = started.timestamp() + total_timeout
        except ValueError:
            pass

    while time.time() < total_deadline:
        meta = _read_meta(project_id)
        if not meta:
            return
        if meta.get("stop_requested"):
            break
        with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
            log_f.write(f"\n=== supervisor loop round={meta.get('round')} phase=crawl ===\n")
        _run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_EXECUTE_PROMPT_PATH, phase="crawl")

        meta = _read_meta(project_id)
        if not meta:
            return
        with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
            log_f.write(f"\n=== supervisor loop round={meta.get('round')} phase=flush ===\n")
        _run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_FLUSH_PROMPT_PATH, phase="flush")
        meta = _read_meta(project_id)
        if not meta:
            return
        meta["round"] = int(meta.get("round") or 0) + 1
        _write_meta(project_id, meta)
        if meta.get("stop_requested"):
            break

    meta = _read_meta(project_id)
    if not meta:
        return
    with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== supervisor phase=final_flush ===\n")
    _run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_FLUSH_PROMPT_PATH, phase="final_flush")
    meta = _read_meta(project_id)
    if not meta:
        return
    meta["status"] = "done"
    meta["phase"] = "idle"
    meta["pid"] = None
    _write_meta(project_id, meta)


def _build_models_json(cfg: dict) -> str:
    model: dict = {
        "id": cfg["pi_model"],
        "name": cfg["pi_model"],
    }
    ctx = cfg.get("pi_context_window", "")
    if ctx:
        try:
            model["contextWindow"] = int(ctx)
        except ValueError:
            pass
    provider = {
        "baseUrl": cfg["pi_base_url"],
        "api": cfg.get("pi_provider_api", "openai-completions"),
        "apiKey": cfg.get("pi_api_key", ""),
        "models": [model],
    }
    return json.dumps({"providers": {"axiom": provider}}, ensure_ascii=False, separators=(",", ":"))


@router.post("/project-groups/{group_id}/crawler/start", response_model=CrawlerStatus, status_code=201)
def start_crawler(group_id: str, body: CrawlerStartRequest):
    """Start a phased crawler supervisor loop for the target URL."""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM project_groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Project group {group_id} not found")

    project_id = group_id  # use group_id as the crawler identifier
    # Check if already running
    meta = _read_meta(project_id)
    if meta.get("pid"):
        pid = meta["pid"]
        if _is_running(pid):
            raise HTTPException(409, f"爬虫进程已在运行中 (pid={pid})")

    if not CRAWLER_EXECUTE_PROMPT_PATH.exists():
        raise HTTPException(500, f"CrawlerExecute.md 不存在: {CRAWLER_EXECUTE_PROMPT_PATH}")
    if not CRAWLER_FLUSH_PROMPT_PATH.exists():
        raise HTTPException(500, f"CrawlerFlush.md 不存在: {CRAWLER_FLUSH_PROMPT_PATH}")

    parent_id = group_id
    ai_dir = f"/tmp/axiom-files/{parent_id}/crawler"
    pathlib.Path(ai_dir).mkdir(parents=True, exist_ok=True)

    session_id = f"crawler-{project_id}"
    agent_dir = f"{CRAWLER_SESSION_DIR}/{project_id}"
    session_dir = f"{agent_dir}/sessions"
    pathlib.Path(session_dir).mkdir(parents=True, exist_ok=True)

    log_path = f"/tmp/axiom-crawler-{project_id}.log"
    now = utcnow()
    runtime = _load_crawler_runtime_settings()
    supervisor_id = f"sup-{project_id}-{uuid.uuid4().hex[:8]}"
    project_root = pathlib.Path(__file__).resolve().parents[3]
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "--project",
            str(project_root),
            "axiom",
            "crawler-supervisor",
            "--project-id",
            project_id,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    _write_meta(project_id, {
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
        "agent_dir": agent_dir,
        "session_dir": session_dir,
        "log": log_path,
    })

    base = pathlib.Path(ai_dir)
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
        sitemap_exists=_schema_output_exists(base, "sitemap.yaml", "site_map.yaml"),
        api_list_exists=_schema_output_exists(base, "api-list.yaml", "api_inventory.yaml"),
        api_page_map_exists=_schema_output_exists(base, "api-page-map.yaml", "page_api_map.yaml"),
    )


@router.get("/project-groups/{group_id}/crawler", response_model=CrawlerStatus)
def get_crawler_status(group_id: str):
    """Return crawler status and output file existence."""
    project_id = group_id
    meta = _read_meta(project_id)
    if not meta:
        return CrawlerStatus(project_id=project_id, status="idle")

    pid = meta.get("pid")
    supervisor_id = meta.get("supervisor_id")
    phase_pid = meta.get("phase_pid")
    if phase_pid and _is_running(phase_pid):
        status = "stopping" if meta.get("stop_requested") else str(meta.get("status") or "running")
    elif meta.get("status") in {"running", "stopping"}:
        status = "done" if _log_contains_final_summary(str(meta.get("log") or "")) else "failed"
        meta["status"] = status
        meta["phase"] = "idle"
        meta["pid"] = None
        meta["phase_pid"] = None
        _write_meta(project_id, meta)
    else:
        status = str(meta.get("status") or "idle")

    ai_dir = meta.get("output_dir", "")
    base = pathlib.Path(ai_dir) if ai_dir else pathlib.Path("/nonexistent")
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
        sitemap_exists=_schema_output_exists(base, "sitemap.yaml", "site_map.yaml"),
        api_list_exists=_schema_output_exists(base, "api-list.yaml", "api_inventory.yaml"),
        api_page_map_exists=_schema_output_exists(base, "api-page-map.yaml", "page_api_map.yaml"),
    )


@router.post("/project-groups/{group_id}/crawler/stop")
def stop_crawler(group_id: str):
    """Request stop and let crawler enter final flush."""
    project_id = group_id
    meta = _read_meta(project_id)
    phase_pid = meta.get("phase_pid")
    if not meta.get("status") in {"running", "stopping"}:
        raise HTTPException(404, "没有运行中的爬虫进程")
    if phase_pid and not _is_running(phase_pid):
        raise HTTPException(404, f"阶段进程 {phase_pid} 已退出")
    meta["stop_requested"] = True
    meta["status"] = "stopping"
    _write_meta(project_id, meta)
    return {"stopped": True, "pid": phase_pid, "stop_requested": True}
