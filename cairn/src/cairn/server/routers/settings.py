from fastapi import APIRouter, HTTPException
import json
from pathlib import Path

import yaml

from cairn.keyring import encrypt_secret
from cairn.server.db import get_conn
from cairn.server.models import (
    DispatchSettings,
    DispatchWorkerSettings,
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
    workers = []
    for item in data.get("workers", []) or []:
        env = item.get("env") or {}
        workers.append(
            DispatchWorkerSettings(
                name=item.get("name", ""),
                task_types=list(item.get("task_types") or []),
                max_running=item.get("max_running", 1),
                priority=item.get("priority", 0),
                pi_model=env.get("PI_MODEL", ""),
                pi_base_url=env.get("PI_BASE_URL", ""),
                pi_provider_api=env.get("PI_PROVIDER_API", ""),
                pi_model_context_window=env.get("PI_MODEL_CONTEXT_WINDOW", ""),
            )
        )
    settings = get_settings()
    return DispatchSettings(
        server=data.get("server", ""),
        common_env=data.get("common_env") or {},
        runtime=data.get("runtime") or {},
        tasks=data.get("tasks") or {},
        workers=workers,
        proxy_url=settings.proxy_url,
        proxy_bootstrap_enabled=settings.proxy_bootstrap_enabled,
        proxy_explore_enabled=settings.proxy_explore_enabled,
        domain_blacklist=settings.domain_blacklist,
    )


@router.put("/settings/dispatch", response_model=DispatchSettings)
def update_dispatch_settings(body: DispatchSettings):
    current = _read_dispatch_yaml()
    current["server"] = body.server
    current["common_env"] = body.common_env
    current["runtime"] = body.runtime
    current["tasks"] = body.tasks
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
    settings = get_settings()
    update_settings(
        settings.model_copy(
            update={
                "intent_timeout": int((body.tasks.get("explore") or {}).get("timeout", settings.intent_timeout)),
                "reason_timeout": int((body.tasks.get("reason") or {}).get("timeout", settings.reason_timeout)),
                "proxy_url": body.proxy_url,
                "proxy_bootstrap_enabled": body.proxy_bootstrap_enabled,
                "proxy_explore_enabled": body.proxy_explore_enabled,
                "domain_blacklist": body.domain_blacklist,
            }
        )
    )
    return body


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
