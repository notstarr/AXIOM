from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.dispatcher.schemas import ClaimedIntent, DispatcherSnapshot
from axiom.modules.reason.schemas import ClaimedReasonRun
from axiom.modules.plan.schemas import ClaimedPlanRun
from axiom.modules.vulnerability_reviews.schemas import ClaimedVulnerabilityReviewRun
from axiom.modules.dispatch.schemas import (
    ClaimIntent,
    DispatchStallObservation,
    RecoverStaleIntents,
    RecoveryReport,
    ResetRetryBudgetReport,
)
from axiom.modules.tasks.schemas import RunDeadlineReconcileReport


router = APIRouter(prefix="/dispatch", tags=["dispatch"])


@router.post("/task-deadlines/reconcile", response_model=RunDeadlineReconcileReport)
def reconcile_task_deadlines(
    container: Container = Depends(get_container),
) -> RunDeadlineReconcileReport:
    return container.tasks.reconcile_run_deadlines()


@router.post("/tasks/{task_id}/reset-retry-budget", response_model=ResetRetryBudgetReport)
def reset_task_retry_budget(
    task_id: str,
    container: Container = Depends(get_container),
) -> ResetRetryBudgetReport:
    """Clear the retry budget of every pending work item of one Task.

    One button for the operator-facing case "this Task is stuck": all four
    dispatch queues are unblocked at once, so the workers can claim the work
    again instead of waiting out the recovery quiet period.
    """

    outcome = container.dispatch.reset_task_retry_budget(task_id)
    return ResetRetryBudgetReport(
        task_id=task_id,
        reset_by_phase=outcome.reset_by_phase,
        total=outcome.total,
    )


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
    "/tasks/{task_id}/plan-runs/{plan_run_id}/claim",
    response_model=ClaimedPlanRun,
)
def claim_plan_run(
    task_id: str,
    plan_run_id: str,
    body: ClaimIntent,
    container: Container = Depends(get_container),
) -> ClaimedPlanRun:
    return container.dispatch.claim_plan_run(
        task_id=task_id,
        plan_run_id=plan_run_id,
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


@router.post("/recover-parked", response_model=RecoveryReport)
def recover_parked_work(
    container: Container = Depends(get_container),
) -> RecoveryReport:
    """Re-arm work parked at its infrastructure retry limit, once.

    The Runner does this on every tick; this endpoint exists so an operator can
    trigger one pass immediately (for example right after upgrading, when the
    pre-existing parked rows carry no parking timestamp and are therefore not
    eligible yet).
    """

    return container.dispatch.recover_parked()


@router.post("/dispatch-stall", response_model=DispatchStallObservation)
def report_dispatch_stall(
    body: DispatchStallObservation,
    container: Container = Depends(get_container),
) -> DispatchStallObservation:
    """Record that a Task has been idle with unclaimed work.

    Called by the Runner once a Task crosses the stall threshold; it writes one
    audit record and changes nothing else.
    """

    return container.dispatch.observe_stall(body)
