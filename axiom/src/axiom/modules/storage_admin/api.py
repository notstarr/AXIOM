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


@router.post("/refresh", response_model=StorageOverview)
def refresh_storage_overview(
    actor: str = AUTO_ROTATE_ACTOR,
    container: Container = Depends(get_container),
) -> StorageOverview:
    """同步重建统计（含到期轮转）。大目录遍历可能耗时数十秒。"""
    return container.storage_admin.refresh(actor=actor.strip() or "human.frontend")


@router.post("/logs/rotate", response_model=LogRotateResult)
def rotate_storage_logs(
    actor: str = AUTO_ROTATE_ACTOR,
    container: Container = Depends(get_container),
) -> LogRotateResult:
    return container.storage_admin.rotate_logs(actor=actor.strip() or "human.frontend")
