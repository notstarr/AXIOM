from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

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


@router.get("/red-team/results", response_model=list[RedTeamResult])
def list_all_red_team_results(
    response: Response,
    task_id: str | None = Query(default=None),
    chain_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    result_type: str | None = Query(default=None, alias="type"),
    search: str = "",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[RedTeamResult]:
    """跨任务成果列表（重要事实·成果视图）。"""

    items, total = container.red_team.list_results_page(
        task_id=task_id,
        chain_id=chain_id,
        status=status_filter,
        result_type=result_type,
        search=search,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/red-team/chains", response_model=list[RedTeamChain])
def list_all_red_team_chains(
    response: Response,
    task_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str = "",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[RedTeamChain]:
    """跨任务攻击链列表（重要事实·成果视图）。"""

    items, total = container.red_team.list_chains_page(
        task_id=task_id,
        status=status_filter,
        search=search,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items
