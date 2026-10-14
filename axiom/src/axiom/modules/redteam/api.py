from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.redteam.schemas import (
    RedTeamChain,
    RedTeamChainCreate,
    RedTeamResult,
    RedTeamResultReview,
    RedTeamResultReportRequest,
)

router = APIRouter(tags=["red-team"])


@router.post("/tasks/{task_id}/red-team/chains", response_model=RedTeamChain, status_code=status.HTTP_201_CREATED)
def create_chain(task_id: str, body: RedTeamChainCreate, created_by: str = "worker.reason", intent_id: str | None = None, container: Container = Depends(get_container)) -> RedTeamChain:
    return container.red_team.create_chain(task_id, body, created_by=created_by, intent_id=intent_id)


@router.get("/tasks/{task_id}/red-team/chains", response_model=list[RedTeamChain])
def list_chains(task_id: str, container: Container = Depends(get_container)) -> list[RedTeamChain]:
    return container.red_team.list_chains(task_id)


@router.get("/tasks/{task_id}/red-team/results", response_model=list[RedTeamResult])
def list_results(task_id: str, chain_id: str | None = Query(default=None), container: Container = Depends(get_container)) -> list[RedTeamResult]:
    return container.red_team.list_results(task_id, chain_id=chain_id)


@router.post("/tasks/{task_id}/intents/{intent_id}/red-team-results", response_model=RedTeamResult, status_code=status.HTTP_201_CREATED)
def report_result(task_id: str, intent_id: str, body: RedTeamResultReportRequest, reported_by: str = "explore", container: Container = Depends(get_container)) -> RedTeamResult:
    return container.red_team.report_result(task_id, intent_id, body.lease_id, body.report, reported_by=reported_by)


@router.post("/tasks/{task_id}/red-team/results/{result_id}/review", response_model=RedTeamResult)
def review_result(task_id: str, result_id: str, body: RedTeamResultReview, container: Container = Depends(get_container)) -> RedTeamResult:
    return container.red_team.review_result(task_id, result_id, body)
