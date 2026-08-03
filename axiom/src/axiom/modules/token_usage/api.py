from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.token_usage.schemas import TokenUsageBatchCommand, TokenUsageBatchResult


router = APIRouter(prefix="/dispatch", tags=["token-usage"])


@router.post(
    "/tasks/{task_id}/intents/{intent_id}/token-usage",
    response_model=TokenUsageBatchResult,
)
def record_intent_token_usage(
    task_id: str,
    intent_id: str,
    body: TokenUsageBatchCommand,
    container: Container = Depends(get_container),
) -> TokenUsageBatchResult:
    return container.token_usage.record_intent_batch(
        task_id=task_id,
        intent_id=intent_id,
        command=body,
    )


@router.post(
    "/tasks/{task_id}/reason-runs/{reason_run_id}/token-usage",
    response_model=TokenUsageBatchResult,
)
def record_reason_token_usage(
    task_id: str,
    reason_run_id: str,
    body: TokenUsageBatchCommand,
    container: Container = Depends(get_container),
) -> TokenUsageBatchResult:
    return container.token_usage.record_reason_batch(
        task_id=task_id,
        reason_run_id=reason_run_id,
        command=body,
    )
