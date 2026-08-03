from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.dashboard.schemas import DashboardSummary
from axiom.modules.token_usage.schemas import TokenRange


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
def dashboard_summary(
    token_range: TokenRange = Query(default="all"),
    model: str = Query(default="all", max_length=300),
    container: Container = Depends(get_container),
) -> DashboardSummary:
    return container.dashboard.summary(token_range=token_range, model=model)
