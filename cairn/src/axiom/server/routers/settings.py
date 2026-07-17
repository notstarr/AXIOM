from fastapi import APIRouter, HTTPException
import json
import logging
import os
from pathlib import Path
import subprocess
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

from axiom.dispatcher.config import WorkerConfig
from axiom.guard.policy import GuardPolicyConfig, evaluate_guard_event
from axiom.shared.keyring import KeyringError, encrypt_secret
from axiom.dispatcher.keyring import load_provider_api_key
from axiom.dispatcher.workers.adapters.pi import PiDriver
from axiom.server.db import get_conn

LOG = logging.getLogger(__name__)
from axiom.server.dispatch_config_store import (
    bump_dispatch_config_version,
    load_dispatch_config_snapshot,
    write_dispatch_settings,
)
from axiom.server.models import (
    DispatchModelsResponse,
    DispatchSettings,
    JudgeUrlBatchRequest,
    JudgeUrlBatchResponse,
    JudgeUrlTestResult,
    JudgeSettingsResponse,
    JudgeTestResponse,
    DispatchRuntimeWorkerView,
    DispatchWorkerSettings,
    PiConnectionTestRequest,
    PiConnectionTestResponse,
    ProviderKeyMeta,
    ProviderKeysResponse,
    Settings,
    UpdateJudgeSettingsRequest,
    UpdateProviderKeyRequest,
)
from axiom.server.judge_settings import DEFAULT_JUDGE_HARD_BLOCK_RULES, DEFAULT_JUDGE_SYSTEM_PROMPT, JudgeSettingsData, load_judge_settings, save_judge_settings
from axiom.server.services import utcnow


router = APIRouter(tags=["settings"])
SUPPORTED_KEY_PROVIDERS = ("pi", "judge")
DISPATCH_YAML = Path(__file__).resolve().parents[5] / "dispatch.yaml"


def _sanitize_dispatch_task_types(values: list[str] | None) -> list[str]:
    task_types: list[str] = []
    for item in values or []:
        value = str(item)
        if value not in task_types:
            task_types.append(value)
    return task_types or ['bootstrap', 'reason', 'explore']


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT intent_timeout, reason_timeout,
                bootstrap_worker_count, explore_worker_count, reason_worker_count,
                plan_worker_count, plan_timeout,
                crawler_active_window_seconds, crawler_flush_window_seconds, crawler_total_timeout_seconds,
                openai_base_url, openai_model,
                proxy_url, proxy_enabled
            FROM settings
            WHERE rowid = 1
            """
        ).fetchone()
        return Settings(
            intent_timeout=row["intent_timeout"],
            reason_timeout=row["reason_timeout"],
            bootstrap_worker_count=row["bootstrap_worker_count"],
            explore_worker_count=row["explore_worker_count"],
            reason_worker_count=row["reason_worker_count"],
            plan_worker_count=row["plan_worker_count"],
            plan_timeout=row["plan_timeout"],
            crawler_active_window_seconds=row["crawler_active_window_seconds"],
            crawler_flush_window_seconds=row["crawler_flush_window_seconds"],
            crawler_total_timeout_seconds=row["crawler_total_timeout_seconds"],
            openai_base_url=row["openai_base_url"] or "",
            openai_model=row["openai_model"] or "",
            proxy_url=row["proxy_url"] or "",
            proxy_enabled=bool(row["proxy_enabled"]),
        )


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE settings
            SET intent_timeout = ?,
                reason_timeout = ?,
                bootstrap_worker_count = ?,
                explore_worker_count = ?,
                reason_worker_count = ?,
                plan_worker_count = ?,
                plan_timeout = ?,
                crawler_active_window_seconds = ?,
                crawler_flush_window_seconds = ?,
                crawler_total_timeout_seconds = ?,
                openai_base_url = ?,
                openai_model = ?,
                proxy_url = ?,
                proxy_enabled = ?
            WHERE rowid = 1
            """,
            (
                body.intent_timeout,
                body.reason_timeout,
                body.bootstrap_worker_count,
                body.explore_worker_count,
                body.reason_worker_count,
                body.plan_worker_count,
                body.plan_timeout,
                body.crawler_active_window_seconds,
                body.crawler_flush_window_seconds,
                body.crawler_total_timeout_seconds,
                body.openai_base_url,
                body.openai_model,
                body.proxy_url,
                1 if body.proxy_enabled else 0,
            ),
        )
    bump_dispatch_config_version()
    return get_settings()


@router.get("/settings/dispatch", response_model=DispatchSettings)
def get_dispatch_settings():
    snapshot = load_dispatch_config_snapshot(DISPATCH_YAML)
    data = snapshot.config_data
    runtime = data.get("runtime") or {}
    tasks = data.get("tasks") or {}
    bootstrap = tasks.get("bootstrap") or {}
    explore = tasks.get("explore") or {}
    reason = tasks.get("reason") or {}
    workers = []
    for item in data.get("workers", []) or []:
        env = item.get("env") or {}
        worker_name = str(item.get("name", "")).strip()
        workers.append(
            DispatchWorkerSettings(
                name=worker_name,
                task_types=_sanitize_dispatch_task_types([str(t) for t in (item.get("task_types") or [])]),
                max_running=item.get("max_running", 1),
                priority=item.get("priority", 0),
                pi_model=env.get("PI_MODEL", ""),
                pi_base_url=env.get("PI_BASE_URL", ""),
                pi_provider_api=env.get("PI_PROVIDER_API", ""),
                pi_model_context_window=env.get("PI_MODEL_CONTEXT_WINDOW", ""),
                pi_api_key="",
                pi_api_key_masked=_mask_secret_middle(env.get("PI_API_KEY", "")) or _provider_key_hint(_worker_key_provider(worker_name)),
                pi_thinking=_normalize_pi_thinking(env.get("PI_THINKING", "off")),
                pi_thinking_capable=_normalize_thinking_capable(env.get("PI_THINKING_CAPABLE", "unknown")),
                pi_thinking_tested_at=env.get("PI_THINKING_TESTED_AT", ""),
                mcp_enabled=_as_bool(env.get("PI_MCP_ENABLED", "")),
                mcp_profile=env.get("PI_MCP_PROFILE", ""),
                mcp_config_json=env.get("PI_MCP_CONFIG_JSON", ""),
                mcp_allowed_servers=env.get("PI_MCP_ALLOWED_SERVERS", ""),
                mcp_allowed_tools=env.get("PI_MCP_ALLOWED_TOOLS", ""),
                disabled=bool(item.get("disabled")),
            )
        )
    settings = get_settings()
    runtime_state = snapshot.runtime_state
    effective_workers = []
    for item in (runtime_state.workers if runtime_state is not None else []):
        effective_workers.append(
            DispatchRuntimeWorkerView(
                name=str(item.get("name", "")),
                task_types=_sanitize_dispatch_task_types([str(task_type) for task_type in (item.get("task_types") or [])]),
                max_running=max(1, int(item.get("max_running", 1) or 1)),
                priority=max(0, int(item.get("priority", 0) or 0)),
                disabled=bool(item.get("disabled")),
            )
        )
    proxy_url, proxy_username, proxy_password = _split_proxy_url(settings.proxy_url)
    return DispatchSettings(
        runtime_interval=runtime.get("interval", 3),
        runtime_max_workers=runtime.get("max_workers", 8),
        runtime_max_running_projects=runtime.get("max_running_projects", 3),
        runtime_max_project_workers=runtime.get("max_project_workers", 4),
        runtime_healthcheck_timeout=runtime.get("healthcheck_timeout", 20),
        runtime_worker_healthcheck=runtime.get("worker_healthcheck", "startup_only"),
        runtime_prompt_group=runtime.get("prompt_group", "default"),
        bootstrap_timeout=bootstrap.get("timeout", 300),
        bootstrap_conclude_timeout=bootstrap.get("conclude_timeout", 90),
        explore_timeout=explore.get("timeout", 300),
        explore_conclude_timeout=explore.get("conclude_timeout", 90),
        reason_timeout=reason.get("timeout", 300),
        reason_max_intents=reason.get("max_intents", 2),
        depth_reason_max_intents=reason.get("depth_max_intents", 5),
        workers=workers,
        proxy_url=proxy_url,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_enabled=settings.proxy_enabled,
        config_version=snapshot.version.version,
        config_updated_at=snapshot.version.updated_at,
        dispatcher_applied_version=runtime_state.config_version if runtime_state is not None else 0,
        dispatcher_applied_at=runtime_state.applied_at if runtime_state is not None else "",
        effective_workers=effective_workers,
        bootstrap_worker_count=settings.bootstrap_worker_count,
        explore_worker_count=settings.explore_worker_count,
        reason_worker_count=settings.reason_worker_count,
    )


@router.put("/settings/dispatch", response_model=DispatchSettings)
def update_dispatch_settings(body: DispatchSettings):
    proxy_url = _compose_proxy_url(body.proxy_url, body.proxy_username, body.proxy_password)
    db_worker_envs: dict[str, dict[str, str]] = {}
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT name, env_json FROM dispatch_workers"
            ).fetchall()
            for row in rows:
                try:
                    env = json.loads(row["env_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    env = {}
                if isinstance(env, dict):
                    db_worker_envs[str(row["name"])] = {
                        str(k): str(v) for k, v in env.items()
                    }
    except Exception:
        pass
    resolved_workers: list[dict[str, object]] = []
    for worker in body.workers:
        _validate_worker_mcp_inputs(worker)
        key = (worker.pi_api_key or "").strip()
        if not key:
            db_env = db_worker_envs.get(worker.name, {})
            if db_env.get("PI_API_KEY"):
                key = db_env["PI_API_KEY"]
        if not key:
            key = load_provider_api_key(_worker_key_provider(worker.name)) or ""
        if not key:
            key = load_provider_api_key("pi") or ""
        resolved_workers.append({
            "name": worker.name,
            "type": "pi",
            "task_types": _sanitize_dispatch_task_types(list(worker.task_types)),
            "max_running": worker.max_running,
            "priority": worker.priority,
            "disabled": worker.disabled,
            "env": {
                "PI_MODEL": worker.pi_model,
                "PI_BASE_URL": worker.pi_base_url,
                "PI_API_KEY": key,
                "PI_PROVIDER_API": worker.pi_provider_api,
                "PI_MODEL_CONTEXT_WINDOW": worker.pi_model_context_window,
                "PI_THINKING": worker.pi_thinking,
                "PI_THINKING_CAPABLE": worker.pi_thinking_capable,
                "PI_THINKING_TESTED_AT": worker.pi_thinking_tested_at,
                "PI_MCP_ENABLED": "true" if worker.mcp_enabled else "false",
                "PI_MCP_PROFILE": worker.mcp_profile or "",
                "PI_MCP_CONFIG_JSON": worker.mcp_config_json or "",
                "PI_MCP_ALLOWED_SERVERS": worker.mcp_allowed_servers or "",
                "PI_MCP_ALLOWED_TOOLS": worker.mcp_allowed_tools or "",
            },
        })
    write_dispatch_settings(
        {
            "runtime_interval": body.runtime_interval,
            "runtime_max_workers": body.runtime_max_workers,
            "runtime_max_running_projects": body.runtime_max_running_projects,
            "runtime_max_project_workers": body.runtime_max_project_workers,
            "runtime_healthcheck_timeout": body.runtime_healthcheck_timeout,
            "runtime_worker_healthcheck": body.runtime_worker_healthcheck,
            "runtime_prompt_group": body.runtime_prompt_group,
            "bootstrap_timeout": body.bootstrap_timeout,
            "bootstrap_conclude_timeout": body.bootstrap_conclude_timeout,
            "explore_timeout": body.explore_timeout,
            "explore_conclude_timeout": body.explore_conclude_timeout,
            "reason_timeout": body.reason_timeout,
            "reason_max_intents": body.reason_max_intents,
            "depth_reason_max_intents": body.depth_reason_max_intents,
            "bootstrap_worker_count": body.bootstrap_worker_count,
            "explore_worker_count": body.explore_worker_count,
            "reason_worker_count": body.reason_worker_count,
            "proxy_url": proxy_url,
            "proxy_enabled": body.proxy_enabled,
            "workers": resolved_workers,
        }
    )
    return get_dispatch_settings()


@router.put("/settings/dispatch/workers/{worker_name}/toggle", response_model=DispatchSettings)
def toggle_worker_disabled(worker_name: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT disabled FROM dispatch_workers WHERE name = ?",
            (worker_name,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"worker not found: {worker_name}")
        new_disabled = 0 if row["disabled"] else 1
        conn.execute(
            "UPDATE dispatch_workers SET disabled = ? WHERE name = ?",
            (new_disabled, worker_name),
        )
    bump_dispatch_config_version()
    return get_dispatch_settings()


@router.post("/settings/test-pi", response_model=PiConnectionTestResponse)
def test_pi_connection(body: PiConnectionTestRequest):
    api_key = body.pi_api_key.strip()
    if not api_key and body.worker_name:
        worker_env = _load_dispatch_worker_env(body.worker_name)
        api_key = worker_env.get("PI_API_KEY", "")
    if not api_key and body.worker_name:
        api_key = load_provider_api_key(_worker_key_provider(body.worker_name)) or ""
    if not api_key:
        api_key = load_provider_api_key("pi") or ""
    if not api_key:
        return PiConnectionTestResponse(ok=False, message="未设置 PI API Key，请先保存密钥")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        if body.pi_provider_api == "openai-responses":
            response = requests.post(
                f"{body.pi_base_url}/responses",
                headers=headers,
                json={"model": body.pi_model, "input": "ping", "max_output_tokens": 8},
                timeout=15,
            )
        else:
            response = requests.post(
                f"{body.pi_base_url}/chat/completions",
                headers=headers,
                json={
                    "model": body.pi_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                    "temperature": 0,
                },
                timeout=15,
            )
    except requests.RequestException as exc:
        return PiConnectionTestResponse(ok=False, message=f"连接失败：{exc}")
    if 200 <= response.status_code < 300:
        thinking_capable, thinking_message = _probe_pi_thinking(body, api_key)
        tested_at = utcnow()
        suffix = f"，{thinking_message}" if thinking_message else ""
        return PiConnectionTestResponse(
            ok=True,
            message=f"连接成功，模型可用{suffix}",
            thinking_capable=thinking_capable,
            thinking_tested_at=tested_at,
            thinking_message=thinking_message,
        )
    detail = response.text[:500] if response.text else response.reason
    return PiConnectionTestResponse(ok=False, message=f"HTTP {response.status_code}: {detail}")


def _probe_pi_thinking(body: PiConnectionTestRequest, api_key: str) -> tuple[str, str]:
    worker_env = _load_dispatch_worker_env(body.worker_name)
    probe_name = f"thinking-probe-{_safe_worker_name(body.worker_name or body.pi_model)}"
    env = {
        "PI_MODEL": body.pi_model,
        "PI_BASE_URL": body.pi_base_url,
        "PI_API_KEY": api_key,
        "PI_PROVIDER_API": body.pi_provider_api,
        "PI_THINKING": "high",
    }
    if worker_env.get("PI_MODEL_CONTEXT_WINDOW"):
        env["PI_MODEL_CONTEXT_WINDOW"] = worker_env["PI_MODEL_CONTEXT_WINDOW"]
    if worker_env.get("PI_BINARY_PATH"):
        env["PI_BINARY_PATH"] = worker_env["PI_BINARY_PATH"]
    try:
        worker = WorkerConfig.model_validate(
            {
                "name": probe_name,
                "type": "pi",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": env,
            }
        )
        driver = PiDriver()
        session = driver.prepare_session()
        command = driver.build_execute(
            worker,
            "Use brief internal reasoning if available, then reply with exactly: pong",
            session,
            phase="explore",
        ).argv
        process_env = os.environ.copy()
        process_env.update(env)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
            env=process_env,
        )
    except subprocess.TimeoutExpired:
        return "unknown", "思考检测超时"
    except Exception as exc:
        LOG.debug("pi thinking probe failed worker=%s error=%s", body.worker_name, exc)
        return "unknown", "思考检测失败"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        preview = detail[-1][:120] if detail else "pi 返回非零状态"
        LOG.debug("pi thinking probe nonzero worker=%s rc=%s detail=%s", body.worker_name, result.returncode, preview)
        return "unknown", "思考检测失败"
    if _has_reasoning_signal(result.stdout) or _has_session_reasoning_signal(probe_name, session):
        return "true", "思考可用"
    return "false", "思考未生效"


def _has_session_reasoning_signal(worker_name: str, session: str | None) -> bool:
    if not session:
        return False
    session_dir = Path("/tmp/axiom-pi") / worker_name / "sessions"
    if not session_dir.is_dir():
        return False
    for path in sorted(session_dir.glob(f"*{session}*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _json_has_reasoning(event):
                        return True
        except OSError:
            continue
    return False


def _has_reasoning_signal(stdout: str) -> bool:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _json_has_reasoning(event):
            return True
    return False


def _json_has_reasoning(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if "reasoning" in key_text:
                if isinstance(item, str) and item.strip():
                    return True
                if isinstance(item, (int, float)) and item > 0:
                    return True
                if isinstance(item, dict) and _usage_has_reasoning_tokens(item):
                    return True
            if key_text in {"thinking", "thinking_content"}:
                if isinstance(item, str) and item.strip().lower() not in {"off", "none", "false", "0"}:
                    return True
                if isinstance(item, dict) and _json_has_reasoning(item):
                    return True
            if key_text == "usage" and isinstance(item, dict) and _usage_has_reasoning_tokens(item):
                return True
            if _json_has_reasoning(item):
                return True
    elif isinstance(value, list):
        return any(_json_has_reasoning(item) for item in value)
    return False


def _usage_has_reasoning_tokens(usage: dict) -> bool:
    for key, value in usage.items():
        key_text = str(key).lower()
        if "reasoning" in key_text and isinstance(value, (int, float)) and value > 0:
            return True
        if isinstance(value, dict) and _usage_has_reasoning_tokens(value):
            return True
    return False


@router.get("/settings/dispatch/models", response_model=DispatchModelsResponse)
def list_dispatch_models(base_url: str = "", api_key: str = "", worker_name: str = ""):
    worker = worker_name.strip()
    worker_env = _load_dispatch_worker_env(worker) if worker else {}
    effective_base_url = (base_url or "").strip() or worker_env.get("PI_BASE_URL", "")
    if not effective_base_url:
        return DispatchModelsResponse(models=[], error="base_url 未配置")

    effective_api_key = (api_key or "").strip()
    if not effective_api_key and worker:
        effective_api_key = worker_env.get("PI_API_KEY", "")
    if not effective_api_key and worker:
        effective_api_key = load_provider_api_key(_worker_key_provider(worker)) or ""
    if not effective_api_key:
        effective_api_key = load_provider_api_key("pi") or ""

    url = effective_base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if effective_api_key:
        headers["Authorization"] = f"Bearer {effective_api_key}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        return DispatchModelsResponse(models=[], error=f"请求失败: {exc}")
    if response.status_code != 200:
        return DispatchModelsResponse(models=[], error=f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except Exception:
        return DispatchModelsResponse(models=[], error="响应不是合法 JSON")

    raw_models: list[dict] = []
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        raw_models = payload["data"]
    elif isinstance(payload, list):
        raw_models = payload

    models = []
    seen_ids: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or "").strip()
        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        context_window = (
            item.get("context_length")
            or item.get("context_window")
            or item.get("max_context_length")
            or 1000000
        )
        try:
            context_window_int = int(context_window)
        except Exception:
            context_window_int = 128000
        if context_window_int <= 0:
            context_window_int = 128000
        models.append({"id": model_id, "context_window": context_window_int})
    return DispatchModelsResponse(models=models, error="")


def _worker_key_provider(worker_name: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in worker_name.strip())
    return f"pi:{safe_name or 'default'}"


def _safe_worker_name(value: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value.strip())
    return safe_name or "default"


def _load_dispatch_worker_env(worker_name: str) -> dict[str, str]:
    if not worker_name:
        return {}
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT env_json FROM dispatch_workers WHERE name = ?",
                (worker_name,),
            ).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    try:
        env = json.loads(row["env_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def _as_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "enabled"}


def _normalize_pi_thinking(value: str | None) -> str:
    text = str(value or "off").strip().lower()
    if text in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        return text
    return "off"


def _normalize_thinking_capable(value: str | None) -> str:
    text = str(value or "unknown").strip().lower()
    if text in {"unknown", "true", "false"}:
        return text
    return "unknown"


def _validate_worker_mcp_inputs(worker: DispatchWorkerSettings) -> None:
    if not worker.mcp_enabled:
        return
    raw = (worker.mcp_config_json or "").strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"worker {worker.name} mcp_config_json 必须是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, f"worker {worker.name} mcp_config_json 必须是 JSON 对象")


def _save_provider_key(provider: str, api_key: str, updated_by: str) -> None:
    now = utcnow()
    ciphertext = _encrypt_provider_secret(api_key)
    hint = api_key[-4:]
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO provider_keys (provider, key_ciphertext, key_hint, is_active, updated_at, updated_by)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                key_ciphertext=excluded.key_ciphertext,
                key_hint=excluded.key_hint,
                is_active=1,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
            """,
            (provider, ciphertext, hint, now, updated_by),
        )


def _encrypt_provider_secret(api_key: str) -> str:
    try:
        return encrypt_secret(api_key)
    except KeyringError as exc:
        raise HTTPException(status_code=500, detail=f"密钥加密失败：{exc}") from exc


def _provider_key_hint(provider: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT key_hint, is_active FROM provider_keys WHERE provider = ?",
            (provider,),
        ).fetchone()
    if row is None or not bool(row["is_active"]) or not row["key_hint"]:
        return ""
    return f"****{row['key_hint']}"


def _mask_secret_middle(secret: str) -> str:
    text = (secret or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    keep = 4
    masked_len = max(4, len(text) - keep * 2)
    return f"{text[:keep]}{'*' * masked_len}{text[-keep:]}"


def _split_proxy_url(proxy_url: str) -> tuple[str, str, str]:
    text = (proxy_url or "").strip()
    if not text:
        return "", "", ""
    parts = urlsplit(text)
    if not parts.username and not parts.password:
        return text, "", ""
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    host = parts.hostname or ""
    netloc = host
    if parts.port is not None:
        netloc = f"{host}:{parts.port}"
    clean_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return clean_url, username, password


def _compose_proxy_url(proxy_url: str, username: str, password: str) -> str:
    text = (proxy_url or "").strip()
    if not text or not username.strip():
        return text
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text
    userinfo = quote(username.strip(), safe="")
    if password.strip():
        userinfo = f"{userinfo}:{quote(password.strip(), safe='')}"
    host = parts.hostname or ""
    netloc = f"{userinfo}@{host}"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _judge_settings_response(settings: JudgeSettingsData) -> JudgeSettingsResponse:
    masked = _provider_key_hint("judge")
    return JudgeSettingsResponse(
        enabled_default=settings.enabled_default,
        mode=settings.mode,
        base_url=settings.base_url,
        model=settings.model,
        timeout_seconds=settings.timeout_seconds,
        whitelist_tools=settings.whitelist_tools,
        whitelist_bash_verbs=settings.whitelist_bash_verbs,
        system_prompt=settings.system_prompt,
        default_system_prompt=DEFAULT_JUDGE_SYSTEM_PROMPT,
        hard_block_rules=settings.hard_block_rules,
        default_hard_block_rules=DEFAULT_JUDGE_HARD_BLOCK_RULES,
        has_key=bool(masked),
        masked_key=masked,
        updated_at=settings.updated_at,
    )


@router.get("/settings/judge", response_model=JudgeSettingsResponse)
def get_judge_settings():
    with get_conn() as conn:
        return _judge_settings_response(load_judge_settings(conn))


@router.put("/settings/judge", response_model=JudgeSettingsResponse)
def update_judge_settings(body: UpdateJudgeSettingsRequest):
    with get_conn() as conn:
        current = load_judge_settings(conn)
        saved = save_judge_settings(
            conn,
            JudgeSettingsData(
                enabled_default=body.enabled_default,
                mode=body.mode,
                base_url=body.base_url,
                model=body.model,
                timeout_seconds=body.timeout_seconds,
                whitelist_tools=body.whitelist_tools,
                whitelist_bash_verbs=body.whitelist_bash_verbs,
                system_prompt=body.system_prompt if "system_prompt" in body.model_fields_set else current.system_prompt,
                hard_block_rules=[rule.model_dump() for rule in body.hard_block_rules] if "hard_block_rules" in body.model_fields_set else current.hard_block_rules,
            ),
        )
        return _judge_settings_response(saved)


@router.post("/settings/judge/test", response_model=JudgeTestResponse)
def test_judge_settings():
    with get_conn() as conn:
        settings = load_judge_settings(conn)
    api_key = load_provider_api_key("judge") or os.getenv("AXIOM_GUARD_API_KEY") or os.getenv("SILICONFLOW_API_KEY")
    if not api_key:
        return JudgeTestResponse(ok=False, message="裁判 API Key 未配置")
    started = __import__("time").perf_counter()
    try:
        response = requests.post(
            f"{settings.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": settings.system_prompt or DEFAULT_JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": "只回复一行 ALLOW"},
                ],
                "temperature": 0,
                "max_tokens": 16,
                "enable_thinking": False,
            },
            timeout=settings.timeout_seconds,
        )
        latency_ms = int((__import__("time").perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return JudgeTestResponse(ok=False, message=f"HTTP {response.status_code}: {response.text[:300]}", latency_ms=latency_ms)
        payload = response.json()
        raw = str(payload.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        return JudgeTestResponse(ok=raw == "ALLOW", decision=raw, message="连接成功" if raw else "模型未返回内容", latency_ms=latency_ms, raw_output=raw[:200])
    except Exception as exc:
        latency_ms = int((__import__("time").perf_counter() - started) * 1000)
        return JudgeTestResponse(ok=False, message=f"{type(exc).__name__}: {exc}", latency_ms=latency_ms)


@router.post("/settings/judge/batch-url-test", response_model=JudgeUrlBatchResponse)
def test_judge_urls(body: JudgeUrlBatchRequest):
    """批量 URL 守门测试：复用 evaluate_guard_event，强制 enforce 模式（忽略全局开关）。"""
    with get_conn() as conn:
        settings = load_judge_settings(conn)
    api_key = load_provider_api_key("judge")
    config = GuardPolicyConfig(
        mode="enforce",  # 强制判定，不受全局 judge 模式影响
        base_url=settings.base_url,
        model_name=settings.model,
        timeout_seconds=settings.timeout_seconds,
        whitelist_tools=settings.whitelist_tools,
        whitelist_bash_verbs=settings.whitelist_bash_verbs,
        hard_block_rules=settings.hard_block_rules,
        system_prompt=settings.system_prompt or DEFAULT_JUDGE_SYSTEM_PROMPT,
    )
    results: list[JudgeUrlTestResult] = []
    allowed = blocked = 0
    total_latency = 0
    for item in body.items:
        input_payload = {"method": item.method, "url": item.url, "body": item.body}
        evaluation = evaluate_guard_event(
            tool_name="http_request",
            input_payload=input_payload,
            metadata={"source": "settings.judge.batch-url-test"},
            config=config,
            api_key=api_key,
        )
        results.append(
            JudgeUrlTestResult(
                method=item.method,
                url=item.url,
                body=item.body,
                decision=evaluation.decision,
                reason=evaluation.reason,
                source=evaluation.source,
                model_name=evaluation.model_name,
                model_latency_ms=evaluation.model_latency_ms,
                raw_model_output=evaluation.raw_model_output[:2000],
                normalized=evaluation.normalized,
            )
        )
        if evaluation.decision == "BLOCK":
            blocked += 1
        else:
            allowed += 1
        total_latency += evaluation.model_latency_ms
    return JudgeUrlBatchResponse(
        results=results,
        total=len(results),
        allowed=allowed,
        blocked=blocked,
        total_latency_ms=total_latency,
    )


@router.get("/settings/keys", response_model=ProviderKeysResponse)
def list_provider_keys():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT provider, key_hint, is_active, updated_at, updated_by FROM provider_keys"
        ).fetchall()
        by_provider = {row["provider"]: row for row in rows}
        items: list[ProviderKeyMeta] = []
        for provider in SUPPORTED_KEY_PROVIDERS:
            row = by_provider.get(provider)
            if row is None:
                items.append(
                    ProviderKeyMeta(
                        provider=provider,
                        has_key=False,
                        masked_key="",
                        is_active=False,
                    )
                )
                continue
            hint = row["key_hint"] or ""
            items.append(
                ProviderKeyMeta(
                    provider=provider,
                    has_key=bool(hint),
                    masked_key=f"****{hint}" if hint else "",
                    is_active=bool(row["is_active"]),
                    updated_at=row["updated_at"],
                    updated_by=row["updated_by"],
                )
            )
        return ProviderKeysResponse(items=items)


@router.put("/settings/keys/{provider}", response_model=ProviderKeyMeta)
def update_provider_key(provider: str, body: UpdateProviderKeyRequest):
    if provider not in SUPPORTED_KEY_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    now = utcnow()
    ciphertext = _encrypt_provider_secret(body.api_key)
    hint = body.api_key[-4:]
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO provider_keys (provider, key_ciphertext, key_hint, is_active, updated_at, updated_by)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                key_ciphertext=excluded.key_ciphertext,
                key_hint=excluded.key_hint,
                is_active=1,
                updated_at=excluded.updated_at,
                updated_by=excluded.updated_by
            """,
            (provider, ciphertext, hint, now, body.updated_by),
        )
    return ProviderKeyMeta(
        provider=provider,
        has_key=True,
        masked_key=f"****{hint}",
        is_active=True,
        updated_at=now,
        updated_by=body.updated_by,
    )
