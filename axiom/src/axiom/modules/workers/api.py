from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.axiom_tools.schemas import WorkerOverview
from axiom.modules.workers.schemas import (
    UpsertWorker,
    Worker,
    WorkerConnectionRequest,
    WorkerConnectionResult,
    WorkerCooldown,
    WorkerModelsResult,
)


router = APIRouter(prefix="/workers", tags=["workers"])


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
    container: Container = Depends(get_container),
) -> WorkerModelsResult:
    return container.workers.list_models(name, base_url=base_url, api_key=api_key)


@router.post("/{name}/test", response_model=WorkerConnectionResult)
def test_worker_connection(
    name: str,
    body: WorkerConnectionRequest,
    container: Container = Depends(get_container),
) -> WorkerConnectionResult:
    return container.workers.test_connection(name, body)


@router.post("/{name}/cooldown", response_model=Worker)
def set_worker_cooldown(
    name: str,
    body: WorkerCooldown,
    container: Container = Depends(get_container),
) -> Worker:
    return container.workers.set_cooldown(name, body.cooldown_until)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_worker(name: str, container: Container = Depends(get_container)) -> Response:
    container.workers.delete(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
