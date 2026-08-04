from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from axiom.modules.reason.schemas import ClaimedReasonRun
from axiom.modules.vulnerability_reviews.schemas import ClaimedVulnerabilityReviewRun
from axiom.modules.dispatch.schemas import ClaimIntent, RecoverStaleIntents, RecoveryReport
from axiom.modules.tasks.schemas import RunDeadlineReconcileReport


router = APIRouter(prefix="/dispatch", tags=["dispatch"])


@router.post("/task-deadlines/reconcile", response_model=RunDeadlineReconcileReport)
def reconcile_task_deadlines(
    container: Container = Depends(get_container),
) -> RunDeadlineReconcileReport:
    return container.tasks.reconcile_run_deadlines()


@router.get("/snapshot", response_model=DispatcherSnapshot)
def get_dispatch_snapshot(
    container: Container = Depends(get_container),
) -> DispatcherSnapshot:
    return container.dispatch.snapshot()


@router.post(
    "/tasks/{task_id}/intents/{intent_id}/claim",
    response_model=ClaimedIntent,
)
def claim_intent(
    task_id: str,
    intent_id: str,
    body: ClaimIntent,
    container: Container = Depends(get_container),
) -> ClaimedIntent:
    return container.dispatch.claim_intent(
        task_id=task_id,
        intent_id=intent_id,
        worker_name=body.worker_name,
    )


@router.post(
    "/tasks/{task_id}/reason-runs/{reason_run_id}/claim",
    response_model=ClaimedReasonRun,
)
def claim_reason_run(
    task_id: str,
    reason_run_id: str,
    body: ClaimIntent,
    container: Container = Depends(get_container),
) -> ClaimedReasonRun:
    return container.dispatch.claim_reason_run(
        task_id=task_id,
        reason_run_id=reason_run_id,
        worker_name=body.worker_name,
    )


@router.post(
    "/tasks/{task_id}/vulnerability-review-runs/{review_run_id}/claim",
    response_model=ClaimedVulnerabilityReviewRun,
)
def claim_vulnerability_review_run(
    task_id: str,
    review_run_id: str,
    body: ClaimIntent,
    container: Container = Depends(get_container),
) -> ClaimedVulnerabilityReviewRun:
    return container.dispatch.claim_review_run(
        task_id=task_id,
        review_run_id=review_run_id,
        worker_name=body.worker_name,
    )


@router.post("/recover-stale", response_model=RecoveryReport)
def recover_stale_intents(
    body: RecoverStaleIntents,
    container: Container = Depends(get_container),
) -> RecoveryReport:
    return container.dispatch.recover_stale(
        stale_after_seconds=body.stale_after_seconds,
    )
