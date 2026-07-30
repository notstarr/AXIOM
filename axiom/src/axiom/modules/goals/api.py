from __future__ import annotations

from fastapi import APIRouter, Depends, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.goals.schemas import AssessGoal, GoalCycle, GoalEvaluation


router = APIRouter(prefix="/tasks", tags=["goals"])


@router.get("/{task_id}/goal-cycles", response_model=list[GoalCycle])
def list_goal_cycles(
    task_id: str,
    container: Container = Depends(get_container),
) -> list[GoalCycle]:
    return container.goals.list_cycles(task_id)


@router.get("/{task_id}/goal-evaluations", response_model=list[GoalEvaluation])
def list_goal_evaluations(
    task_id: str,
    container: Container = Depends(get_container),
) -> list[GoalEvaluation]:
    return container.goals.list(task_id)


@router.post(
    "/{task_id}/goal-evaluations",
    response_model=GoalEvaluation,
    status_code=status.HTTP_201_CREATED,
)
def assess_goal(
    task_id: str,
    body: AssessGoal,
    container: Container = Depends(get_container),
) -> GoalEvaluation:
    return container.goals.assess(task_id, body)
