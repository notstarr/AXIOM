from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.context.schemas import (
    BuildContext,
    ContextRevision,
    ExpandContext,
    ExpandedContext,
)


router = APIRouter(prefix="/tasks", tags=["context"])


@router.post("/{task_id}/context", response_model=ContextRevision)
def build_context(
    task_id: str,
    body: BuildContext,
    container: Container = Depends(get_container),
) -> ContextRevision:
    return container.context.build(
        task_id=task_id,
        phase=body.phase,
        intent_id=body.intent_id,
        reason_run_id=body.reason_run_id,
        plan_run_id=body.plan_run_id,
        review_run_id=body.review_run_id,
        lease_id=body.lease_id,
    )


@router.post("/{task_id}/context/expand", response_model=ExpandedContext)
def expand_context(
    task_id: str,
    body: ExpandContext,
    container: Container = Depends(get_container),
) -> ExpandedContext:
    return container.context.expand(task_id, body.refs, phase=body.phase)
