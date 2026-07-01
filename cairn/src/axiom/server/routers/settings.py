from fastapi import APIRouter, HTTPException
import json
import logging
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

from axiom.shared.keyring import KeyringError, encrypt_secret
from axiom.dispatcher.keyring import load_provider_api_key
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
    DispatchRuntimeWorkerView,
    DispatchWorkerSettings,
    PiConnectionTestRequest,
    PiConnectionTestResponse,
    ProviderKeyMeta,
    ProviderKeysResponse,
    Settings,
    UpdateProviderKeyRequest,
)
from axiom.server.services import utcnow

router = APIRouter(tags=["settings"])
SUPPORTED_KEY_PROVIDERS = ("pi",)
DISPATCH_YAML = Path(__file__).resolve().parents[5] / "dispatch.yaml"


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
                proxy_url, proxy_enabled, domain_blacklist_json
            FROM settings
            WHERE rowid = 1
            """
        ).fetchone()
        domain_blacklist: list[str] = []
        try:
            payload = json.loads(row["domain_blacklist_json"] or "[]")
            if isinstance(payload, list):
                domain_blacklist = [str(item) for item in payload]
        except json.JSONDecodeError:
            domain_blacklist = []
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
            domain_blacklist=domain_blacklist,
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
                proxy_enabled = ?,
                domain_blacklist_json = ?
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
                json.dumps(body.domain_blacklist, ensure_ascii=False, separators=(",", ":")),
            ),
        )
    bump_dispatch_config_version()
    return body


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
                task_types=list(item.get("task_types") or []),
                max_running=item.get("max_running", 1),
                priority=item.get("priority", 0),
                pi_model=env.get("PI_MODEL", ""),
                pi_base_url=env.get("PI_BASE_URL", ""),
                pi_provider_api=env.get("PI_PROVIDER_API", ""),
                pi_model_context_window=env.get("PI_MODEL_CONTEXT_WINDOW", ""),
                pi_api_key="",
                pi_api_key_masked=_mask_secret_middle(env.get("PI_API_KEY", "")) or _provider_key_hint(_worker_key_provider(worker_name)),
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
                task_types=[str(task_type) for task_type in (item.get("task_types") or [])],
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
            "task_types": worker.task_types,
            "max_running": worker.max_running,
            "priority": worker.priority,
            "disabled": worker.disabled,
            "env": {
                "PI_MODEL": worker.pi_model,
                "PI_BASE_URL": worker.pi_base_url,
                "PI_API_KEY": key,
                "PI_PROVIDER_API": worker.pi_provider_api,
                "PI_MODEL_CONTEXT_WINDOW": worker.pi_model_context_window,
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
        return PiConnectionTestResponse(ok=True, message="连接成功，模型可用")
    detail = response.text[:500] if response.text else response.reason
    return PiConnectionTestResponse(ok=False, message=f"HTTP {response.status_code}: {detail}")


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


def _save_provider_key(provider: str, api_key: str, updated_by: str) -> None:
    now = utcnow()
    ciphertext = encrypt_secret(api_key)
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
    ciphertext = encrypt_secret(body.api_key)
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
