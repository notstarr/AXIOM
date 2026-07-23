from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.dispatcher.v2.schemas import DispatcherSnapshot
from axiom.dispatcher.v2.recovery import LeaseRecoveryService, RecoveryResult
from axiom.dispatcher.v2.snapshot import DispatcherSnapshotService
from axiom.modules.settings.schemas import RuntimeConfig, RuntimeSettings
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.workers.schemas import UpsertWorker, Worker
from axiom.modules.workers.service import WorkerService


router = APIRouter(tags=["v1-dispatcher-settings"])
internal = APIRouter(
    prefix="/internal/dispatcher",
    tags=["v1-internal-dispatcher"],
    dependencies=[Depends(require_service_token)],
)


class WorkerCooldownCommand(BaseModel):
    failure_kind: str = ""
    cooldown_until: str | None = None


class RecoverLeasesCommand(BaseModel):
    timeout_seconds: int


class WorkerConnectionRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    provider_api: str = "openai-completions"


class WorkerConnectionResult(BaseModel):
    ok: bool
    message: str


class WorkerModelsResult(BaseModel):
    models: list[dict] = Field(default_factory=list)
    error: str = ""


@router.get("/settings/runtime", response_model=RuntimeSettings)
def get_runtime_settings(engine=Depends(get_v2_engine)):
    return RuntimeSettingsService(engine).get()


@router.put("/settings/runtime", response_model=RuntimeSettings)
def update_runtime_settings(body: RuntimeConfig, actor: str = "human", engine=Depends(get_v2_engine)):
    return RuntimeSettingsService(engine).update(body, actor=actor)


@router.get("/settings/workers", response_model=list[Worker])
def list_workers(engine=Depends(get_v2_engine)):
    return WorkerService(engine).list()


@router.put("/settings/workers/{worker_name}", response_model=Worker)
def upsert_worker(worker_name: str, body: UpsertWorker, engine=Depends(get_v2_engine)):
    data = body.model_copy(update={"name": worker_name})
    return WorkerService(engine).upsert(data)


@router.delete("/settings/workers/{worker_name}", status_code=204)
def delete_worker(worker_name: str, engine=Depends(get_v2_engine)) -> None:
    try:
        WorkerService(engine).delete(worker_name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _worker_connection(body: WorkerConnectionRequest, worker_name: str, engine) -> WorkerConnectionRequest:
    if not worker_name:
        return body
    try:
        worker = WorkerService(engine).get(worker_name)
    except ValueError:
        if body.base_url:
            return body
        raise
    config = worker.config or {}
    env = config.get("env") if isinstance(config.get("env"), dict) else config
    return WorkerConnectionRequest(
        base_url=body.base_url or str(env.get("PI_BASE_URL") or env.get("base_url") or ""),
        api_key=body.api_key or str(env.get("PI_API_KEY") or env.get("api_key") or ""),
        model=body.model or str(env.get("PI_MODEL") or env.get("model") or ""),
        provider_api=body.provider_api
        or str(env.get("PI_PROVIDER_API") or env.get("provider_api") or "openai-completions"),
    )


@router.post("/settings/workers/{worker_name}/test", response_model=WorkerConnectionResult)
def test_worker_connection(
    worker_name: str,
    body: WorkerConnectionRequest,
    engine=Depends(get_v2_engine),
) -> WorkerConnectionResult:
    try:
        effective = _worker_connection(body, worker_name, engine)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not effective.base_url or not effective.model:
        return WorkerConnectionResult(ok=False, message="base_url 和 model 必填")
    headers = {"Content-Type": "application/json"}
    if effective.api_key:
        headers["Authorization"] = f"Bearer {effective.api_key}"
    responses_api = effective.provider_api == "openai-responses"
    path = "/responses" if responses_api else "/chat/completions"
    payload = (
        {"model": effective.model, "input": "ping", "max_output_tokens": 8}
        if responses_api
        else {
            "model": effective.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "temperature": 0,
        }
    )
    try:
        response = httpx.post(
            effective.base_url.rstrip("/") + path,
            headers=headers,
            json=payload,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        return WorkerConnectionResult(ok=False, message=f"连接失败：{exc}")
    if 200 <= response.status_code < 300:
        return WorkerConnectionResult(ok=True, message="连接成功，模型可用")
    return WorkerConnectionResult(
        ok=False,
        message=f"HTTP {response.status_code}: {response.text[:500]}",
    )


@router.get("/settings/workers/{worker_name}/models", response_model=WorkerModelsResult)
def list_worker_models(
    worker_name: str,
    base_url: str = "",
    api_key: str = "",
    engine=Depends(get_v2_engine),
) -> WorkerModelsResult:
    try:
        effective = _worker_connection(
            WorkerConnectionRequest(base_url=base_url, api_key=api_key),
            worker_name,
            engine,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not effective.base_url:
        return WorkerModelsResult(error="base_url 未配置")
    headers = {"Authorization": f"Bearer {effective.api_key}"} if effective.api_key else {}
    try:
        response = httpx.get(
            effective.base_url.rstrip("/") + "/models",
            headers=headers,
            timeout=10,
        )
    except httpx.HTTPError as exc:
        return WorkerModelsResult(error=f"请求失败: {exc}")
    if response.status_code != 200:
        return WorkerModelsResult(error=f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError:
        return WorkerModelsResult(error="响应不是合法 JSON")
    raw = payload.get("data", []) if isinstance(payload, dict) else payload
    models = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            models.append({"id": item, "name": item})
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            if model_id:
                models.append({"id": model_id, "name": model_id})
    return WorkerModelsResult(models=models)


@internal.get("/snapshot", response_model=DispatcherSnapshot)
def dispatcher_snapshot(engine=Depends(get_v2_engine)):
    return DispatcherSnapshotService(engine).build()


@internal.post("/workers/{worker_name}/cooldown", response_model=Worker)
def set_worker_cooldown(
    worker_name: str,
    body: WorkerCooldownCommand,
    engine=Depends(get_v2_engine),
):
    return WorkerService(engine).set_cooldown(
        worker_name,
        failure_kind=body.failure_kind,
        cooldown_until=body.cooldown_until,
    )


@internal.post("/recover-stale-leases", response_model=RecoveryResult)
def recover_stale_leases(body: RecoverLeasesCommand, engine=Depends(get_v2_engine)):
    return LeaseRecoveryService(engine).recover(timeout_seconds=max(1, body.timeout_seconds))
