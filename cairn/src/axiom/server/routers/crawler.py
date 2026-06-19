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
import re
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

from axiom.dispatcher.output_parser import extract_json_object
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
CRAWLER_TRAFFIC_FILE = pathlib.Path("/tmp/proxy_flows.jsonl")
_CRAWL_TOOLS = "bash,read_file,write_file,list_dir"
_FLUSH_TOOLS = "bash,read_file,write_file,list_dir"
_CRAWLER_VIEW_FILES = {
    "site_map.yaml": ("crawler_pages", "pages"),
    "api_inventory.yaml": ("crawler_apis", "apis"),
    "page_api_map.yaml": ("crawler_page_maps", "mappings"),
}
# Stateless CDP helper (replaces the unstable browser-use daemon/session layer).
_CDP_PY = "python3"
_CDP_CTL = "repo-root/tools/crawler/cdp_ctl.py"
_CDP_ENDPOINT = "http://localhost:9223"


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


def _cdp_ctl(*args: str, timeout: float = 30.0) -> tuple[int, str]:
    """Run the stateless CDP helper and return (returncode, combined stdout)."""
    try:
        proc = subprocess.run(
            [_CDP_PY, _CDP_CTL, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 127, f"cdp_ctl invocation failed: {e}"


def _create_crawler_tab(target_url: str) -> str:
    """Create a dedicated Chrome tab for the crawler via the CDP helper.

    Returns the CDP target id, or "" if the browser is unreachable.
    """
    rc, out = _cdp_ctl("new-tab", target_url, timeout=30.0)
    if rc != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("TAB_ID:"):
            return line.split(":", 1)[1].strip()
    return ""


def _close_crawler_tab(tab_id: str) -> None:
    if not tab_id:
        return
    _cdp_ctl("close-tab", tab_id, timeout=10.0)


def _ensure_crawler_tab(meta: dict) -> str:
    """Ensure a live dedicated CDP tab exists for this crawler run.

    Reuses the tab id in meta if it still responds; otherwise creates a new one
    and persists it into meta. Returns the tab id (may be "" if Chrome is down).
    """
    project_id = str(meta["project_id"])
    tab_id = str(meta.get("cdp_tab_id") or "")
    if tab_id:
        rc, _ = _cdp_ctl("--tab-id", tab_id, "url", timeout=8.0)
        if rc == 0:
            return tab_id
    target_url = str(meta.get("target_url") or "")
    tab_id = _create_crawler_tab(target_url) if target_url else ""
    if tab_id:
        meta["cdp_tab_id"] = tab_id
        _write_meta(project_id, meta)
    return tab_id


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


def _crawler_state_id_column(table: str) -> str:
    return {
        "crawler_pages": "page_id",
        "crawler_apis": "api_id",
        "crawler_page_maps": "map_id",
    }[table]


def _load_crawler_state_rows(group_id: str, table: str) -> list[dict[str, Any]]:
    id_column = _crawler_state_id_column(table)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT data_json FROM {table} WHERE group_id = ? ORDER BY updated_at, {id_column}",
            (group_id,),
        ).fetchall()
    loaded: list[dict[str, Any]] = []
    for row in rows:
        try:
            parsed = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            loaded.append(parsed)
    return loaded


def _crawler_state_has_rows(group_id: str, table: str) -> bool:
    id_column = _crawler_state_id_column(table)
    try:
        with get_conn() as conn:
            row = conn.execute(
                f"SELECT {id_column} FROM {table} WHERE group_id = ? LIMIT 1",
                (group_id,),
            ).fetchone()
    except AssertionError:
        return False
    return row is not None


def _crawler_target_url_for_group(group_id: str) -> str:
    meta = _read_meta(group_id)
    target_url = str(meta.get("target_url") or "").strip()
    if target_url:
        return target_url
    return ""


def _render_crawler_state_snapshot(group_id: str, filename: str, *, target_url: str | None = None) -> str:
    if filename not in _CRAWLER_VIEW_FILES:
        raise FileNotFoundError(filename)
    table, root_key = _CRAWLER_VIEW_FILES[filename]
    target = target_url or _crawler_target_url_for_group(group_id)
    payload = {
        "version": 1,
        "target": {
            "url": target,
            "domain": _normalize_target_domain(target),
            "started_at": utcnow(),
        },
        root_key: _load_crawler_state_rows(group_id, table),
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _crawler_state_view_exists(group_id: str, filename: str) -> bool:
    if filename not in _CRAWLER_VIEW_FILES:
        return False
    table, _root_key = _CRAWLER_VIEW_FILES[filename]
    return _crawler_state_has_rows(group_id, table)


def _unwrap_accepted_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    accepted = payload.get("accepted")
    if accepted is False:
        return None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("crawler data must be an object")
        return data
    return payload if isinstance(payload, dict) else None


def _validate_crawler_increment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = _unwrap_accepted_payload(payload)
    if data is None:
        raise RuntimeError("crawler payload rejected")
    increment = data.get("increment", data)
    if not isinstance(increment, dict):
        raise ValueError("crawler increment must be an object")
    normalized = {
        "description": str(increment.get("description") or data.get("description") or "").strip(),
        "pages": increment.get("pages") or [],
        "apis": increment.get("apis") or [],
        "mappings": increment.get("mappings") or [],
    }
    for key in ("pages", "apis", "mappings"):
        if not isinstance(normalized[key], list):
            raise ValueError(f"{key} must be an array")
        for index, item in enumerate(normalized[key]):
            if not isinstance(item, dict):
                raise ValueError(f"{key}[{index}] must be an object")
            if not item.get("id"):
                raise ValueError(f"{key}[{index}].id is required")
    if not any(normalized[key] for key in ("pages", "apis", "mappings")):
        raise ValueError("crawler increment cannot be empty")
    return normalized


def _extract_phase_increment_payload(session_dir: str, *, log_path: str | None = None, log_offset: int = 0) -> dict[str, Any] | None:
    def _try_candidates(text: str) -> dict[str, Any] | None:
        for candidate in reversed([line.strip() for line in text.splitlines() if line.strip().startswith("{") and line.strip().endswith("}")]):
            try:
                payload = json.loads(candidate)
                return _validate_crawler_increment_payload(payload)
            except Exception:
                continue
        try:
            payload = extract_json_object(text)
            return _validate_crawler_increment_payload(payload)
        except Exception:
            return None

    directory = pathlib.Path(session_dir)
    if directory.exists():
        files = sorted(
            [path for path in directory.iterdir() if path.is_file() and path.suffix == ".jsonl"],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            assistant_texts: list[str] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("type") != "message":
                    continue
                body = message.get("message") or {}
                if body.get("role") != "assistant":
                    continue
                for part in body.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        assistant_texts.append(str(part.get("text") or ""))
            for candidate in reversed(assistant_texts):
                payload = _try_candidates(candidate)
                if payload is not None:
                    return payload
    if log_path:
        try:
            with pathlib.Path(log_path).open("r", encoding="utf-8") as fh:
                if log_offset > 0:
                    fh.seek(log_offset)
                text = fh.read()
        except OSError:
            return None
        return _try_candidates(text)
    return None


def _find_existing_page_by_url(conn, group_id: str, url: str) -> str | None:
    """Find an existing page_id by normalized URL. Returns page_id or None."""
    norm = _normalize_url(url)
    if not norm:
        return None
    rows = conn.execute(
        "SELECT page_id, data_json FROM crawler_pages WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if _normalize_url(str(data.get("url") or "")) == norm:
            return str(row["page_id"])
    return None


def _api_signature(method: str, path: str, full_url: str = "") -> str:
    """Build a dedup signature for an API: method + normalized_path + ajax_flag.

    Same path with and without ?isAjax=true are different endpoints (one returns
    HTML, the other returns JSON), so we include an ajax flag in the signature.
    """
    norm_path = _normalize_api_path(path)
    # Detect AJAX variant: if the full URL has isAjax or the path itself differs
    # from a plain page load by having query params that indicate an API call.
    ajax = ""
    if full_url and "isAjax" in full_url:
        ajax = ":ajax"
    return f"{method.upper()} {norm_path}{ajax}"


def _find_existing_api_by_method_path(conn, group_id: str, method: str, path: str,
                                      full_url: str = "") -> str | None:
    """Find an existing api_id by method + normalized path + ajax flag."""
    target_sig = _api_signature(method, path, full_url)
    rows = conn.execute(
        "SELECT api_id, data_json FROM crawler_apis WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        existing_method = str(data.get("method") or "").upper()
        existing_path = str(data.get("path") or "")
        existing_url = str(data.get("full_url_example") or "")
        existing_sig = _api_signature(existing_method, existing_path, existing_url)
        if existing_sig == target_sig:
            return str(row["api_id"])
    return None


def _find_existing_mapping_by_page(conn, group_id: str, page_url: str) -> str | None:
    """Find an existing map_id by normalized page URL."""
    norm = _normalize_url(page_url)
    if not norm:
        return None
    rows = conn.execute(
        "SELECT map_id, data_json FROM crawler_page_maps WHERE group_id = ?",
        (group_id,),
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if _normalize_url(str(data.get("page_url") or "")) == norm:
            return str(row["map_id"])
    return None


def _apply_crawler_increment(group_id: str, increment: dict[str, Any]) -> tuple[int, int, int]:
    new_pages = 0
    new_apis = 0
    new_maps = 0
    with get_conn() as conn:
        for page in increment.get("pages", []):
            page_url = str(page.get("url") or "")
            page_id = str(page["id"])
            # Dedup: find existing page by normalized URL
            existing_id = _find_existing_page_by_url(conn, group_id, page_url) if page_url else None
            if existing_id:
                # Merge into existing record under its original id
                _upsert_crawler_state_row(conn, "crawler_pages", group_id, existing_id, page)
            else:
                # Check if this exact id already exists
                exists = conn.execute(
                    "SELECT 1 FROM crawler_pages WHERE group_id = ? AND page_id = ?",
                    (group_id, page_id),
                ).fetchone()
                if not exists:
                    new_pages += 1
                _upsert_crawler_state_row(conn, "crawler_pages", group_id, page_id, page)

        for api in increment.get("apis", []):
            method = str(api.get("method") or "GET").upper()
            path = str(api.get("path") or "/")
            full_url = str(api.get("full_url_example") or api.get("url") or "")
            api_id = str(api["id"])
            # Dedup: find existing API by method + normalized path + ajax flag
            existing_id = _find_existing_api_by_method_path(conn, group_id, method, path, full_url)
            if existing_id:
                _upsert_crawler_state_row(conn, "crawler_apis", group_id, existing_id, api)
            else:
                exists = conn.execute(
                    "SELECT 1 FROM crawler_apis WHERE group_id = ? AND api_id = ?",
                    (group_id, api_id),
                ).fetchone()
                if not exists:
                    new_apis += 1
                _upsert_crawler_state_row(conn, "crawler_apis", group_id, api_id, api)

        for mapping in increment.get("mappings", []):
            page_url = str(mapping.get("page_url") or "")
            map_id = str(mapping["id"])
            # Dedup: find existing mapping by normalized page URL
            existing_id = _find_existing_mapping_by_page(conn, group_id, page_url) if page_url else None
            if existing_id:
                _upsert_crawler_state_row(conn, "crawler_page_maps", group_id, existing_id, mapping)
            else:
                exists = conn.execute(
                    "SELECT 1 FROM crawler_page_maps WHERE group_id = ? AND map_id = ?",
                    (group_id, map_id),
                ).fetchone()
                if not exists:
                    new_maps += 1
                _upsert_crawler_state_row(conn, "crawler_page_maps", group_id, map_id, mapping)
    return (new_pages, new_apis, new_maps)


def _upsert_crawler_state_row(conn, table: str, group_id: str, row_id: str, payload: dict[str, Any]) -> None:
    id_column = _crawler_state_id_column(table)
    existing = conn.execute(
        f"SELECT data_json FROM {table} WHERE group_id = ? AND {id_column} = ?",
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
        INSERT INTO {table} (group_id, {id_column}, data_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id, {id_column}) DO UPDATE SET
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


def _log_contains_success_summary(log_path: str, *, offset: int = 0) -> bool:
    path = pathlib.Path(log_path)
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            if offset > 0:
                fh.seek(offset)
            text = fh.read()
    except OSError:
        return False
    return '"accepted": true' in text and "description" in text


def _log_contains_final_summary(log_path: str) -> bool:
    return _log_contains_success_summary(log_path)


def _normalize_target_domain(target_url: str) -> str:
    return urlsplit(target_url.strip()).hostname or ""


def _slug_token(value: str, *, default: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or default


def _normalize_url(url: str) -> str:
    """Normalize a URL for dedup: lowercase host, drop fragment, strip trailing slash, sort query params."""
    if not url:
        return ""
    try:
        parsed = urlsplit(url.strip())
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    # Sort query params for stable comparison
    if parsed.query:
        from urllib.parse import parse_qsl, urlencode
        params = sorted(parse_qsl(parsed.query, keep_blank_values=True))
        query = urlencode(params)
    else:
        query = ""
    return f"{parsed.scheme}://{host}{path}" + (f"?{query}" if query else "")


def _normalize_api_path(path: str) -> str:
    """Normalize an API path: replace numeric/hex/uuid segments and {template} placeholders with {id}."""
    if not path:
        return "/"
    segments = path.strip("/").split("/")
    normalized = []
    for seg in segments:
        if not seg:
            continue
        # Pure digits
        if re.match(r"^\d+$", seg):
            normalized.append("{id}")
        # UUID-like
        elif re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", seg, re.I):
            normalized.append("{id}")
        # Long hex (32+ chars)
        elif re.match(r"^[0-9a-f]{32,}$", seg, re.I):
            normalized.append("{id}")
        # Template placeholders like {uuid}, {id}, {article_id}, {group_id}
        elif re.match(r"^\{[a-z_]+\}$", seg, re.I):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    return "/" + "/".join(normalized) if normalized else "/"


def _iso_to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime, timezone

        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _traffic_since_epoch(meta: dict[str, Any]) -> float:
    return _iso_to_epoch(str(meta.get("last_flush_at") or "")) or _iso_to_epoch(str(meta.get("started_at") or ""))


def _load_recent_traffic_flows(meta: dict[str, Any]) -> list[dict[str, Any]]:
    path = CRAWLER_TRAFFIC_FILE
    if not path.exists():
        return []
    lower_bound = _traffic_since_epoch(meta)
    flows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    flow = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(flow, dict):
                    continue
                try:
                    ts = float(flow.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts and ts < lower_bound:
                    continue
                flows.append(flow)
    except OSError:
        return []
    return flows


def _flow_referer_url(flow: dict[str, Any]) -> str:
    headers = flow.get("request_headers") or {}
    if not isinstance(headers, dict):
        return ""
    referer = headers.get("referer") or headers.get("Referer") or ""
    return str(referer).strip()


def _flow_is_relevant_api(flow: dict[str, Any]) -> bool:
    method = str(flow.get("method") or "").upper()
    if method in {"", "CONNECT", "OPTIONS"}:
        return False
    url = str(flow.get("url") or "")
    if "/api/" in url:
        return True
    content_type = str(flow.get("content_type") or "").lower()
    return any(token in content_type for token in ("application/json", "application/xml", "text/xml", "application/graphql", "text/plain"))


def _make_page_record(page_url: str) -> dict[str, Any] | None:
    parsed = urlsplit(page_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path or "/"
    page_slug = _slug_token(f"{parsed.netloc}_{path}", default="page")
    page_type = "form" if path.endswith("/new") else "unknown"
    name_bits = [bit for bit in path.strip("/").split("/") if bit]
    page_name = " / ".join(name_bits) if name_bits else parsed.netloc
    return {
        "id": f"page_{page_slug}",
        "name": page_name,
        "url": page_url,
        "path": path,
        "page_type": page_type,
        "parent_id": None,
        "discovered_via": {
            "type": "unknown",
            "source_page_id": None,
            "trigger_label": "traffic fallback",
        },
        "requires_auth": False,
        "business_role": "unknown",
        "preconditions": [],
        "actions": [],
        "key_data": [],
        "related_api_ids": [],
        "notes": ["traffic fallback"],
        "exhausted": False,
    }


def _make_api_record(flow: dict[str, Any], page: dict[str, Any] | None) -> dict[str, Any] | None:
    url = str(flow.get("url") or "")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    method = str(flow.get("method") or "GET").upper()
    path = parsed.path or "/"
    status_code = int(flow.get("status_code") or 0)
    api_slug = _slug_token(f"{parsed.netloc}_{method}_{path}", default="api")
    response_keys: list[str] = []
    response_body = str(flow.get("response_body") or "")
    try:
        body_json = json.loads(response_body) if response_body else None
        if isinstance(body_json, dict):
            response_keys = [str(key) for key in body_json.keys()]
    except json.JSONDecodeError:
        pass
    headers = flow.get("request_headers") or {}
    cookie_value = ""
    if isinstance(headers, dict):
        cookie_value = str(headers.get("cookie") or headers.get("Cookie") or "")
    notes = ["traffic fallback"]
    if status_code in {401, 403}:
        notes.append("captured as auth failure during flush fallback")
    return {
        "id": f"api_{api_slug}",
        "method": method,
        "path": path,
        "full_url_example": url,
        "purpose": f"{method} {parsed.netloc}{path}",
        "protocol": parsed.scheme,
        "content_type": str(flow.get("content_type") or ""),
        "status_codes_seen": [status_code] if status_code else [],
        "auth": {
            "required": status_code in {401, 403},
            "type": "cookie" if cookie_value else "unknown",
            "example": cookie_value[:120] or None,
        },
        "seen_on_pages": (
            [{"page_id": page["id"], "page_url": page["url"]}] if page else []
        ),
        "params": [],
        "response_keys": response_keys,
        "produces_data": [],
        "depends_on": [],
        "missing_inputs": [],
        "notes": notes,
    }


def _make_map_record(flow: dict[str, Any], page: dict[str, Any], api: dict[str, Any]) -> dict[str, Any]:
    path = str(api.get("path") or "/")
    method = str(api.get("method") or "GET")
    action_slug = _slug_token(f"{method}_{path}", default="action")
    status_code = int((flow.get("status_code") or 0) if str(flow.get("status_code") or "").isdigit() else flow.get("status_code") or 0)
    result_status = "denied" if status_code in {401, 403} else ("failure" if status_code >= 400 else "success")
    return {
        "id": f"map_{page['id'][5:]}" if str(page.get("id", "")).startswith("page_") else f"map_{_slug_token(page['id'], default='page')}",
        "page_id": page["id"],
        "page_name": page["name"],
        "page_url": page["url"],
        "on_load": [],
        "actions": [
            {
                "action_id": f"action_{action_slug}",
                "action_name": f"{method} {path}",
                "trigger_type": "form_submit" if method == "POST" else "click",
                "trigger_label": path,
                "target_hint": "traffic fallback",
                "result": {
                    "navigation": False,
                    "status": result_status,
                },
                "apis": [
                    {
                        "api_id": api["id"],
                        "method": method,
                        "path": path,
                    }
                ],
                "notes": ["traffic fallback"],
            }
        ],
    }


def _merge_recent_traffic_into_state(meta: dict[str, Any]) -> tuple[int, int, int]:
    flows = [flow for flow in _load_recent_traffic_flows(meta) if _flow_is_relevant_api(flow)]
    if not flows:
        return (0, 0, 0)
    project_id = str(meta["project_id"])
    with get_conn() as conn:
        new_pages = 0
        new_apis = 0
        new_maps = 0
        for flow in flows:
            referer_url = _flow_referer_url(flow)
            # Page: skip if a page with this normalized URL already exists
            if referer_url:
                existing_page_id = _find_existing_page_by_url(conn, project_id, referer_url)
                if existing_page_id:
                    page = None  # don't create a duplicate
                else:
                    page = _make_page_record(referer_url)
            else:
                page = None

            api = _make_api_record(flow, page)
            if api is None:
                continue

            # API: skip if an API with same method + normalized path + ajax flag exists
            existing_api_id = _find_existing_api_by_method_path(
                conn, project_id,
                str(api.get("method") or "GET"),
                str(api.get("path") or "/"),
                str(api.get("full_url_example") or ""),
            )
            if existing_api_id:
                # Only merge status codes into existing record
                existing_row = conn.execute(
                    "SELECT data_json FROM crawler_apis WHERE group_id = ? AND api_id = ?",
                    (project_id, existing_api_id),
                ).fetchone()
                if existing_row:
                    try:
                        existing_data = json.loads(existing_row["data_json"] or "{}")
                    except json.JSONDecodeError:
                        existing_data = {}
                    seen = set(existing_data.get("status_codes_seen") or [])
                    for sc in (api.get("status_codes_seen") or []):
                        seen.add(sc)
                    existing_data["status_codes_seen"] = sorted(seen)
                    _upsert_crawler_state_row(conn, "crawler_apis", project_id, existing_api_id, existing_data)
                # Don't count as new
            else:
                api_exists = conn.execute(
                    "SELECT 1 FROM crawler_apis WHERE group_id = ? AND api_id = ?",
                    (project_id, api["id"]),
                ).fetchone()
                if not api_exists:
                    new_apis += 1
                _upsert_crawler_state_row(conn, "crawler_apis", project_id, api["id"], api)

            if page is not None:
                # Mapping: skip if a mapping for this page URL already exists
                existing_map_id = _find_existing_mapping_by_page(conn, project_id, page["url"])
                if existing_map_id:
                    continue  # don't create duplicate mapping
                mapping = _make_map_record(flow, page, api)
                map_exists = conn.execute(
                    "SELECT 1 FROM crawler_page_maps WHERE group_id = ? AND map_id = ?",
                    (project_id, mapping["id"]),
                ).fetchone()
                if not map_exists:
                    new_maps += 1
                _upsert_crawler_state_row(conn, "crawler_page_maps", project_id, mapping["id"], mapping)

            if page is not None and not existing_page_id:
                # Only count as new if we actually created it
                page_exists = conn.execute(
                    "SELECT 1 FROM crawler_pages WHERE group_id = ? AND page_id = ?",
                    (project_id, page["id"]),
                ).fetchone()
                if not page_exists:
                    new_pages += 1
                _upsert_crawler_state_row(conn, "crawler_pages", project_id, page["id"], page)
    return (new_pages, new_apis, new_maps)


def _fallback_persist_recent_traffic(meta: dict[str, Any], log_f) -> bool:
    new_pages, new_apis, new_maps = _merge_recent_traffic_into_state(meta)
    if new_pages == 0 and new_apis == 0 and new_maps == 0:
        return False
    description = f"backend fallback persisted {new_pages} new pages, {new_apis} new apis, {new_maps} new mappings from recent traffic."
    log_f.write(json.dumps({"accepted": True, "data": {"description": description}}, ensure_ascii=False) + "\n")
    log_f.flush()
    return True


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


def _build_crawler_state_context(group_id: str) -> str:
    """Build a compact context string for the prompt: discovered URLs + frontier."""
    pages = _load_crawler_state_rows(group_id, "crawler_pages")
    if not pages:
        return "（暂无已发现页面，这是第一轮探索）"

    # Discovered URLs with type + exhausted flag
    discovered_lines = []
    discovered_urls = set()
    for p in pages:
        url = str(p.get("url") or "")
        if not url:
            continue
        norm = _normalize_url(url)
        if norm in discovered_urls:
            continue
        discovered_urls.add(norm)
        ptype = p.get("page_type") or "unknown"
        exhausted = "已穷尽" if p.get("exhausted") else ""
        tag = f" [{ptype}]{(' ' + exhausted) if exhausted else ''}"
        discovered_lines.append(f"- {url}{tag}")

    # Frontier: extract hrefs from page actions that haven't been visited
    frontier_urls = []
    seen_frontier = set()
    for p in pages:
        for action in (p.get("actions") or []):
            href = str(action.get("href") or action.get("target_hint") or "")
            if not href or not href.startswith("http"):
                continue
            norm = _normalize_url(href)
            if norm in discovered_urls or norm in seen_frontier:
                continue
            seen_frontier.add(norm)
            label = action.get("name") or action.get("label") or ""
            frontier_urls.append(f"- {href}" + (f" ({label})" if label else ""))

    parts = [f"已发现 {len(discovered_lines)} 个页面:"]
    parts.extend(discovered_lines[:40])  # cap at 40 to control token usage
    if len(discovered_lines) > 40:
        parts.append(f"... (另有 {len(discovered_lines) - 40} 个页面省略)")

    if frontier_urls:
        parts.append("")
        parts.append(f"未访问的链接 ({len(frontier_urls)} 个, 建议优先探索):")
        parts.extend(frontier_urls[:15])
        if len(frontier_urls) > 15:
            parts.append(f"... (另有 {len(frontier_urls) - 15} 个未访问链接)")

    return "\n".join(parts)


def _build_prompt(template_path: pathlib.Path, *, session_id: str, target_url: str, ai_dir: str,
                  group_id: str = "", round_num: int = 0, total_rounds: int = 0) -> str:
    prompt_template = template_path.read_text(encoding="utf-8")
    intent_description = f"爬取目标站点 {target_url}，产出站点地图、API 清单和页面-API 映射到 {ai_dir}"
    state_context = _build_crawler_state_context(group_id) if group_id else ""
    round_info = f"当前第 {round_num} 轮" if round_num else ""
    if total_rounds:
        round_info += f", 预计剩余约 {max(0, total_rounds - round_num)} 轮"
    return (
        prompt_template
        .replace("{intent_id}", session_id)
        .replace("{intent_description}", intent_description)
        .replace("{ai_dir}", ai_dir)
        .replace("{crawler_state}", state_context)
        .replace("{round_info}", round_info)
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


def _run_phase_once(*, cfg: dict, meta: dict, prompt_path: pathlib.Path, phase: str) -> str:
    ai_dir = str(meta["output_dir"])
    session_id = str(meta["session_id"])
    agent_dir = str(meta["agent_dir"])
    session_dir = str(meta["session_dir"])
    # The prompt's {intent_id} is the dedicated CDP tab id the agent must drive.
    tab_id = str(meta.get("cdp_tab_id") or "")
    prompt_intent_id = tab_id or session_id
    prompt = _build_prompt(
        prompt_path,
        session_id=prompt_intent_id,
        target_url=str(meta["target_url"]),
        ai_dir=ai_dir,
        group_id=str(meta["project_id"]),
        round_num=int(meta.get("round") or 0),
        total_rounds=int(meta.get("total_timeout_seconds") or 0) // max(1, int(meta.get("active_window_seconds") or 1)),
    )
    tools = _FLUSH_TOOLS if phase in {"flush", "final_flush"} else _CRAWL_TOOLS
    argv = _build_pi_argv(cfg=cfg, agent_dir=agent_dir, session_dir=session_dir, prompt=prompt, tools=tools)
    log_path = str(meta["log"])
    log_offset = pathlib.Path(log_path).stat().st_size if pathlib.Path(log_path).exists() else 0
    phase_started_at = utcnow()
    meta["phase"] = phase
    meta["status"] = "stopping" if meta.get("stop_requested") else "running"
    meta["phase_started_at"] = phase_started_at
    meta["phase_accepted"] = False
    _write_meta(str(meta["project_id"]), meta)

    phase_result = "failed"
    with open(log_path, "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== phase {phase} start {phase_started_at} tab={tab_id or 'none'} ===\n")
        log_f.flush()
        proc = subprocess.Popen(
            argv,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
        meta["phase_pid"] = proc.pid
        _write_meta(str(meta["project_id"]), meta)
        timeout = int(meta["flush_window_seconds"] if phase in {"flush", "final_flush"} else meta["active_window_seconds"])
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
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
            increment = None if timed_out else _extract_phase_increment_payload(session_dir, log_path=log_path, log_offset=log_offset)
            if phase == "crawl":
                if increment is not None:
                    _apply_crawler_increment(str(meta["project_id"]), increment)
                    # Merge traffic to catch APIs the agent didn't report but
                    # were captured in the proxy. The dedup logic in
                    # _merge_recent_traffic_into_state prevents duplicate noise.
                    _merge_recent_traffic_into_state(meta)
                    meta["phase_accepted"] = True
                    meta["status"] = "running"
                    _write_meta(str(meta["project_id"]), meta)
                    phase_result = "accepted"
                else:
                    meta["phase_accepted"] = False
                    meta["status"] = "failed"
                    _write_meta(str(meta["project_id"]), meta)
                    phase_result = "needs_flush"
            else:
                if increment is not None:
                    _apply_crawler_increment(str(meta["project_id"]), increment)
                    _merge_recent_traffic_into_state(meta)
                    meta["phase_accepted"] = True
                    meta["last_flush_at"] = utcnow()
                    meta["status"] = "running"
                    _write_meta(str(meta["project_id"]), meta)
                    phase_result = "accepted"
                elif _fallback_persist_recent_traffic(meta, log_f):
                    meta["phase_accepted"] = True
                    meta["last_flush_at"] = utcnow()
                    meta["status"] = "running"
                    _write_meta(str(meta["project_id"]), meta)
                    phase_result = "accepted"
                else:
                    meta["phase_accepted"] = False
                    meta["status"] = "failed"
                    _write_meta(str(meta["project_id"]), meta)
                    phase_result = "failed"
    return phase_result


def _run_crawler_supervisor(project_id: str) -> None:
    meta = _read_meta(project_id)
    if not meta:
        return
    with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
        log_f.write(f"\n=== supervisor start {utcnow()} project={project_id} ===\n")
    cfg = _load_worker_config()

    # Ensure a dedicated CDP tab exists for this run. Reused across rounds.
    tab_id = _ensure_crawler_tab(meta)
    if tab_id:
        with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
            log_f.write(f"=== crawler tab ready: {tab_id} ===\n")
    else:
        with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
            log_f.write("=== WARNING: no cdp tab (Chrome on 9223 unreachable); crawl phases will fall back ===\n")

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

    try:
        while time.time() < total_deadline:
            meta = _read_meta(project_id)
            if not meta:
                return
            if meta.get("stop_requested"):
                break
            # Re-confirm the tab is alive before each crawl round.
            if not _ensure_crawler_tab(meta):
                with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
                    log_f.write(f"\n=== supervisor loop round={meta.get('round')} no tab; abort crawl ===\n")
                break
            with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
                log_f.write(f"\n=== supervisor loop round={meta.get('round')} phase=crawl ===\n")
            crawl_result = _run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_EXECUTE_PROMPT_PATH, phase="crawl")

            meta = _read_meta(project_id)
            if not meta:
                return
            if crawl_result == "needs_flush":
                with open(str(meta["log"]), "a", encoding="utf-8") as log_f:
                    log_f.write(f"\n=== supervisor loop round={meta.get('round')} phase=flush ===\n")
                flush_result = _run_phase_once(cfg=cfg, meta=meta, prompt_path=CRAWLER_FLUSH_PROMPT_PATH, phase="flush")
                if flush_result != "accepted":
                    meta = _read_meta(project_id)
                    if meta:
                        meta["status"] = "failed"
                        meta["phase"] = "idle"
                        meta["pid"] = None
                        _write_meta(project_id, meta)
                    return

            meta = _read_meta(project_id)
            if not meta:
                return
            meta["round"] = int(meta.get("round") or 0) + 1
            _write_meta(project_id, meta)
            if meta.get("stop_requested"):
                break
    finally:
        meta = _read_meta(project_id)
        if meta:
            _close_crawler_tab(str(meta.get("cdp_tab_id") or ""))
            meta["status"] = "done" if not meta.get("stop_requested") or bool(meta.get("phase_accepted")) else "stopping"
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

    # Close any orphaned dedicated tab from a previous run before starting fresh.
    old_meta = _read_meta(project_id)
    if old_meta.get("cdp_tab_id"):
        _close_crawler_tab(str(old_meta["cdp_tab_id"]))

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
        sitemap_exists=_crawler_state_view_exists(project_id, "site_map.yaml"),
        api_list_exists=_crawler_state_view_exists(project_id, "api_inventory.yaml"),
        api_page_map_exists=_crawler_state_view_exists(project_id, "page_api_map.yaml"),
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
    elif pid and _is_running(pid):
        status = "stopping" if meta.get("stop_requested") else str(meta.get("status") or "running")
    elif meta.get("status") in {"running", "stopping"}:
        status = "done" if str(meta.get("phase") or "idle") == "idle" and bool(meta.get("phase_accepted")) else "failed"
        meta["status"] = status
        meta["phase"] = "idle"
        meta["pid"] = None
        meta["phase_pid"] = None
        _write_meta(project_id, meta)
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
        sitemap_exists=_crawler_state_view_exists(project_id, "site_map.yaml"),
        api_list_exists=_crawler_state_view_exists(project_id, "api_inventory.yaml"),
        api_page_map_exists=_crawler_state_view_exists(project_id, "page_api_map.yaml"),
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


@router.get("/project-groups/{group_id}/crawler/results")
def get_crawler_results(group_id: str):
    """Return structured crawler state as JSON (pages / apis / mappings).

    This is the frontend-friendly endpoint: it returns the raw state rows
    directly from the DB, avoiding the YAML serialization round-trip.
    """
    project_id = group_id
    pages = _load_crawler_state_rows(project_id, "crawler_pages")
    apis = _load_crawler_state_rows(project_id, "crawler_apis")
    mappings = _load_crawler_state_rows(project_id, "crawler_page_maps")
    target = _crawler_target_url_for_group(project_id)
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
