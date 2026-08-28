from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container

from .schemas import (
    CleanupExecuteRequest,
    CleanupExecuteResult,
    CleanupPlan,
    CleanupPreviewRequest,
    CleanupPreviewResponse,
    LogRotateResult,
    StorageOverview,
)
from .service import AUTO_ROTATE_ACTOR

router = APIRouter(prefix="/storage", tags=["storage-admin"])


@router.get("/overview", response_model=StorageOverview)
def get_storage_overview(
    container: Container = Depends(get_container),
) -> StorageOverview:
    return container.storage_admin.overview()


@router.get("/cleanup/plan", response_model=CleanupPlan)
def get_cleanup_plan(
    container: Container = Depends(get_container),
) -> CleanupPlan:
    return container.storage_admin.cleanup_plan()


@router.post("/cleanup/plan/refresh", response_model=CleanupPlan)
def refresh_cleanup_plan(
    container: Container = Depends(get_container),
) -> CleanupPlan:
    return container.storage_admin.refresh_cleanup_plan()


@router.post("/cleanup/preview", response_model=CleanupPreviewResponse)
def preview_cleanup(
    body: CleanupPreviewRequest,
    container: Container = Depends(get_container),
) -> CleanupPreviewResponse:
    items = container.storage_admin.cleanup_preview(body.keys)
    from axiom.shared.clock import utcnow

    return CleanupPreviewResponse(generated_at=utcnow(), items=items)


@router.post("/cleanup/execute", response_model=CleanupExecuteResult)
def execute_cleanup(
    body: CleanupExecuteRequest,
    container: Container = Depends(get_container),
) -> CleanupExecuteResult:
    actor = body.actor.strip() or "human.frontend"
    return container.storage_admin.cleanup_execute(body.keys, body.excluded, actor)


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
