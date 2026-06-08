from fastapi import APIRouter, HTTPException
import json
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests
import yaml

from cairn.keyring import encrypt_secret
from cairn.dispatcher.keyring import load_provider_api_key
from cairn.server.db import get_conn
from cairn.server.models import (
    DispatchSettings,
    DispatchWorkerSettings,
    PiConnectionTestRequest,
    PiConnectionTestResponse,
    ProviderKeyMeta,
    ProviderKeysResponse,
    Settings,
    UpdateProviderKeyRequest,
)
from cairn.server.services import utcnow

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
                openai_base_url, openai_model,
                proxy_url, proxy_bootstrap_enabled, proxy_explore_enabled, domain_blacklist_json
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
            openai_base_url=row["openai_base_url"] or "",
            openai_model=row["openai_model"] or "",
            proxy_url=row["proxy_url"] or "",
            proxy_bootstrap_enabled=bool(row["proxy_bootstrap_enabled"]),
            proxy_explore_enabled=bool(row["proxy_explore_enabled"]),
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
                openai_base_url = ?,
                openai_model = ?,
                proxy_url = ?,
                proxy_bootstrap_enabled = ?,
                proxy_explore_enabled = ?,
                domain_blacklist_json = ?
            WHERE rowid = 1
            """,
            (
                body.intent_timeout,
                body.reason_timeout,
                body.bootstrap_worker_count,
                body.explore_worker_count,
                body.reason_worker_count,
                body.openai_base_url,
                body.openai_model,
                body.proxy_url,
                1 if body.proxy_bootstrap_enabled else 0,
                1 if body.proxy_explore_enabled else 0,
                json.dumps(body.domain_blacklist, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return body


@router.get("/settings/dispatch", response_model=DispatchSettings)
def get_dispatch_settings():
    data = _read_dispatch_yaml()
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
                pi_api_key_masked=_provider_key_hint(_worker_key_provider(worker_name)),
            )
        )
    settings = get_settings()
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
        workers=workers,
        proxy_url=proxy_url,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_bootstrap_enabled=settings.proxy_bootstrap_enabled,
        proxy_explore_enabled=settings.proxy_explore_enabled,
        domain_blacklist=settings.domain_blacklist,
    )


@router.put("/settings/dispatch", response_model=DispatchSettings)
def update_dispatch_settings(body: DispatchSettings):
    current = _read_dispatch_yaml()
    current["runtime"] = {
        "interval": body.runtime_interval,
        "max_workers": body.runtime_max_workers,
        "max_running_projects": body.runtime_max_running_projects,
        "max_project_workers": body.runtime_max_project_workers,
        "healthcheck_timeout": body.runtime_healthcheck_timeout,
        "worker_healthcheck": body.runtime_worker_healthcheck,
        "prompt_group": body.runtime_prompt_group,
    }
    current["tasks"] = {
        "bootstrap": {
            "timeout": body.bootstrap_timeout,
            "conclude_timeout": body.bootstrap_conclude_timeout,
        },
        "reason": {
            "timeout": body.reason_timeout,
            "max_intents": body.reason_max_intents,
        },
        "explore": {
            "timeout": body.explore_timeout,
            "conclude_timeout": body.explore_conclude_timeout,
        },
    }
    current["workers"] = [
        {
            "name": worker.name,
            "type": "pi",
            "task_types": worker.task_types,
            "max_running": worker.max_running,
            "priority": worker.priority,
            "env": {
                "PI_MODEL": worker.pi_model,
                "PI_BASE_URL": worker.pi_base_url,
                "PI_API_KEY": "<stored-in-database>",
                "PI_PROVIDER_API": worker.pi_provider_api,
                "PI_MODEL_CONTEXT_WINDOW": worker.pi_model_context_window,
            },
        }
        for worker in body.workers
    ]
    DISPATCH_YAML.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for worker in body.workers:
        if worker.pi_api_key.strip():
            _save_provider_key(_worker_key_provider(worker.name), worker.pi_api_key, f"worker:{worker.name}")
    proxy_url = _compose_proxy_url(body.proxy_url, body.proxy_username, body.proxy_password)
    settings = get_settings()
    update_settings(
        settings.model_copy(
            update={
                "intent_timeout": body.explore_timeout,
                "reason_timeout": body.reason_timeout,
                "bootstrap_worker_count": _phase_worker_count(body.workers, "bootstrap", settings.bootstrap_worker_count),
                "explore_worker_count": _phase_worker_count(body.workers, "explore", settings.explore_worker_count),
                "reason_worker_count": _phase_worker_count(body.workers, "reason", settings.reason_worker_count),
                "proxy_url": proxy_url,
                "proxy_bootstrap_enabled": body.proxy_bootstrap_enabled,
                "proxy_explore_enabled": body.proxy_explore_enabled,
                "domain_blacklist": body.domain_blacklist,
            }
        )
    )
    return body


@router.post("/settings/test-pi", response_model=PiConnectionTestResponse)
def test_pi_connection(body: PiConnectionTestRequest):
    api_key = body.pi_api_key.strip()
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


def _phase_worker_count(workers: list[DispatchWorkerSettings], phase: str, fallback: int) -> int:
    total = sum(worker.max_running for worker in workers if phase in worker.task_types)
    return max(1, total or fallback)


def _worker_key_provider(worker_name: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in worker_name.strip())
    return f"pi:{safe_name or 'default'}"


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


def _read_dispatch_yaml() -> dict:
    if not DISPATCH_YAML.exists():
        return {}
    data = yaml.safe_load(DISPATCH_YAML.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


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
