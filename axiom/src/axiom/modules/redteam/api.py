from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.redteam.schemas import (
    UpdateRedTeamResult,
    RedTeamChain,
    RedTeamChainCreate,
    RedTeamResult,
    RedTeamResultHumanReview,
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


@router.get("/tasks/{task_id}/red-team/results/{result_id}", response_model=RedTeamResult)
def get_result(
    task_id: str,
    result_id: str,
    container: Container = Depends(get_container),
) -> RedTeamResult:
    """单条成果的完整读取（含复现报文）。

    列表走 ``summary`` 时不带报文，选中某条再按 id 取这一条——一次翻页就不用
    把几十 KB × 50 条的报文全拖回来。
    """
    return container.red_team.get_result(task_id, result_id)


@router.post("/tasks/{task_id}/intents/{intent_id}/red-team-results", response_model=RedTeamResult, status_code=status.HTTP_201_CREATED)
def report_result(task_id: str, intent_id: str, body: RedTeamResultReportRequest, reported_by: str = "explore", container: Container = Depends(get_container)) -> RedTeamResult:
    return container.red_team.report_result(task_id, intent_id, body.lease_id, body.report, reported_by=reported_by)


@router.patch("/tasks/{task_id}/red-team/results/{result_id}", response_model=RedTeamResult)
def update_result(
    task_id: str,
    result_id: str,
    body: UpdateRedTeamResult,
    container: Container = Depends(get_container),
) -> RedTeamResult:
    """人工编辑成果内容；改实质字段会自动重置为审核中并重新排队。"""
    return container.red_team.update_result(task_id, result_id, body)


@router.post("/tasks/{task_id}/red-team/results/{result_id}/review", response_model=RedTeamResult)
def review_result(task_id: str, result_id: str, body: RedTeamResultReview, container: Container = Depends(get_container)) -> RedTeamResult:
    return container.red_team.review_result(task_id, result_id, body)


@router.post("/tasks/{task_id}/red-team/results/{result_id}/human-review", response_model=RedTeamResult)
def human_review_result(
    task_id: str,
    result_id: str,
    body: RedTeamResultHumanReview,
    container: Container = Depends(get_container),
) -> RedTeamResult:
    """人工覆盖审核者的评估结论：原因、状态、积分数量。

    不受终态限制；在途审核 Run 会被撤销，并在审计事件里留下 before/after。
    """
    return container.red_team.override_human_review(task_id, result_id, body)


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
    summary: bool = Query(default=False),
    container: Container = Depends(get_container),
) -> list[RedTeamResult]:
    """跨任务成果列表（重要事实·成果视图）。

    ``summary=1`` 时不返回 ``reproduction``（那条复现过程带着该环全部原始报文，
    几十 KB 一条）。列表只画标题、状态、分值和描述，报文要等选中某条再单取。
    默认仍返回完整内容，既有调用方不受影响。
    """

    items, total, awarded = container.red_team.list_results_page(
        task_id=task_id,
        chain_id=chain_id,
        status=status_filter,
        result_type=result_type,
        search=search,
        limit=limit,
        offset=offset,
        summary=summary,
    )
    response.headers["X-Total-Count"] = str(total)
    # 核定分是筛选集的全量聚合，前端翻页时要用它显示总分而不是对本页求和。
    response.headers["X-Total-Points"] = str(awarded)
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
