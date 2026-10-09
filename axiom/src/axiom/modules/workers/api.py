from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.axiom_tools.schemas import WorkerOverview
from axiom.modules.phase_capabilities.schemas import PhaseBackendsUpdate, WorkerPhase
from axiom.modules.workers.schemas import (
    UpsertWorker,
    Worker,
    WorkerCapabilityReport,
    WorkerConnectionRequest,
    WorkerConnectionResult,
    WorkerCooldown,
    WorkerModelsResult,
    WorkerRename,
)


router = APIRouter(prefix="/workers", tags=["workers"])
phase_router = APIRouter(prefix="/worker-phases", tags=["worker-phases"])


@phase_router.get("", response_model=list[WorkerPhase])
def list_worker_phases(
    container: Container = Depends(get_container),
) -> list[WorkerPhase]:
    return container.workers.phase_overview()


@phase_router.get("/{phase}", response_model=WorkerPhase)
def get_worker_phase(
    phase: str,
    container: Container = Depends(get_container),
) -> WorkerPhase:
    return container.workers.phase_overview(phase)[0]


@phase_router.put("/{phase}/backends", response_model=WorkerPhase)
def save_worker_phase_backends(
    phase: str,
    body: PhaseBackendsUpdate,
    container: Container = Depends(get_container),
) -> WorkerPhase:
    return container.workers.set_phase_backends(phase, body.backends)


@router.get("/overview", response_model=list[WorkerOverview])
def list_worker_overview(
    container: Container = Depends(get_container),
) -> list[WorkerOverview]:
    return container.axiom_tools.worker_overview()


@router.put("/{name}", response_model=Worker)
def upsert_worker(
    name: str,
    body: UpsertWorker,
    container: Container = Depends(get_container),
) -> Worker:
    return container.workers.upsert(name, body)


@router.get("", response_model=list[Worker])
def list_workers(container: Container = Depends(get_container)) -> list[Worker]:
    return container.workers.list()


@router.get("/{name}", response_model=Worker)
def get_worker(name: str, container: Container = Depends(get_container)) -> Worker:
    return container.workers.get(name)


@router.get("/{name}/models", response_model=WorkerModelsResult)
def list_worker_models(
    name: str,
    base_url: str = "",
    api_key: str = "",
    provider_api: str = "",
    container: Container = Depends(get_container),
) -> WorkerModelsResult:
    return container.workers.list_models(
        name,
        base_url=base_url,
        api_key=api_key,
        provider_api=provider_api,
    )


@router.post("/{name}/test", response_model=WorkerConnectionResult)
def test_worker_connection(
    name: str,
    body: WorkerConnectionRequest,
    container: Container = Depends(get_container),
) -> WorkerConnectionResult:
    return container.workers.test_connection(name, body)


@router.post("/{name}/rename", response_model=Worker)
def rename_worker(
    name: str,
    body: WorkerRename,
    container: Container = Depends(get_container),
) -> Worker:
    return container.workers.rename(name, body.new_name)


@router.post("/{name}/cooldown", response_model=Worker)
def set_worker_cooldown(
    name: str,
    body: WorkerCooldown,
    container: Container = Depends(get_container),
) -> Worker:
    return container.workers.set_cooldown(name, body.cooldown_until)


@router.post("/{name}/capability", response_model=Worker)
def set_worker_capability(
    name: str,
    body: WorkerCapabilityReport,
    container: Container = Depends(get_container),
) -> Worker:
    """Persist a probed gateway capability (currently the developer role)."""

    return container.workers.set_capability(name, body.developer_role_supported)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_worker(name: str, container: Container = Depends(get_container)) -> Response:
    container.workers.delete(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
