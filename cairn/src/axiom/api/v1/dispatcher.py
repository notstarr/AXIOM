from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.dispatcher.v2.schemas import DispatcherSnapshot
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


@internal.get("/snapshot", response_model=DispatcherSnapshot)
def dispatcher_snapshot(engine=Depends(get_v2_engine)):
    return DispatcherSnapshotService(engine).build()
