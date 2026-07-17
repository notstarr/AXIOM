from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

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
