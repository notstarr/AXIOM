from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container

from .schemas import LogRotateResult, StorageOverview
from .service import AUTO_ROTATE_ACTOR

router = APIRouter(prefix="/storage", tags=["storage-admin"])


@router.get("/overview", response_model=StorageOverview)
def get_storage_overview(
    container: Container = Depends(get_container),
) -> StorageOverview:
    return container.storage_admin.overview()


@router.post("/logs/rotate", response_model=LogRotateResult)
def rotate_storage_logs(
    actor: str = AUTO_ROTATE_ACTOR,
    container: Container = Depends(get_container),
) -> LogRotateResult:
    return container.storage_admin.rotate_logs(actor=actor.strip() or "human.frontend")
