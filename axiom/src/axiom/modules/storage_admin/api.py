from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container

from .schemas import (
    CleanupExecuteRequest,
    CleanupExecuteResult,
    CleanupPlan,
    CleanupPreviewRequest,
    CleanupPreviewResponse,
    ContextRetentionPlan,
    ContextRetentionPreviewRequest,
    ContextRetentionRunRequest,
    ContextRetentionRunResult,
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


@router.get("/context-retention/plan", response_model=ContextRetentionPlan)
def context_retention_plan(
    container: Container = Depends(get_container),
) -> ContextRetentionPlan:
    plan = container.storage_admin.context_retention_plan()
    return ContextRetentionPlan(
        generated_at=plan.generated_at,
        targets=[asdict(item) for item in plan.targets],
        database_records=plan.database_records,
        reclaimable_count=plan.reclaimable_count,
        protected_count=plan.protected_count,
        orphan_count=plan.orphan_count,
        total_bytes=plan.total_bytes,
    )


@router.post("/context-retention/preview", response_model=ContextRetentionPlan)
def context_retention_preview(
    body: ContextRetentionPreviewRequest,
    container: Container = Depends(get_container),
) -> ContextRetentionPlan:
    plan = container.storage_admin.context_retention_plan()
    targets = plan.targets[: body.limit]
    return ContextRetentionPlan(
        generated_at=plan.generated_at,
        targets=[asdict(item) for item in targets],
        database_records=plan.database_records,
        reclaimable_count=len(targets),
        protected_count=plan.protected_count,
        orphan_count=plan.orphan_count,
        total_bytes=sum(item.bytes for item in targets),
    )


@router.post("/context-retention/run", response_model=ContextRetentionRunResult)
def context_retention_run(
    body: ContextRetentionRunRequest,
    container: Container = Depends(get_container),
) -> ContextRetentionRunResult:
    result = container.storage_admin.context_retention_run(
        dry_run=body.dry_run,
        actor=body.actor.strip() or "human.frontend",
        max_revisions=body.max_revisions,
        max_bytes=body.max_bytes,
    )
    return ContextRetentionRunResult(
        generated_at=result.generated_at,
        dry_run=result.dry_run,
        paused=result.paused,
        removed=[asdict(item) for item in result.removed],
        skipped=result.skipped,
        freed_bytes=result.freed_bytes,
        protected_count=result.protected_count,
        orphan_count=result.orphan_count,
    )


@router.post("/context-retention/pause", status_code=204)
def pause_context_retention(container: Container = Depends(get_container)) -> None:
    container.storage_admin.pause_context_retention()


@router.post("/context-retention/resume", status_code=204)
def resume_context_retention(container: Container = Depends(get_container)) -> None:
    container.storage_admin.resume_context_retention()


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
