from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.directives.schemas import (
    CreateDirective,
    Directive,
    EligibleExplore,
    UpdateDirective,
)


router = APIRouter(prefix="/tasks", tags=["directives"])


@router.get(
    "/{task_id}/directives/eligible-explores",
    response_model=list[EligibleExplore],
)
def list_eligible_explores(
    task_id: str,
    container: Container = Depends(get_container),
) -> list[EligibleExplore]:
    """Pending Explore Intents that can still receive a one-shot directive.

    Claimed/running intents are excluded: their system prompt was already fixed at
    start, and this build deliberately has no mid-run steering channel.
    """
    return container.directives.eligible_explores(task_id)


@router.get("/{task_id}/directives", response_model=list[Directive])
def list_directives(
    task_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    container: Container = Depends(get_container),
) -> list[Directive]:
    return container.directives.list(task_id, status=status_filter)


@router.post(
    "/{task_id}/directives",
    response_model=Directive,
    status_code=status.HTTP_201_CREATED,
)
def create_directive(
    task_id: str,
    body: CreateDirective,
    container: Container = Depends(get_container),
) -> Directive:
    return container.directives.create(task_id, body)


@router.patch(
    "/{task_id}/directives/{directive_id}", response_model=Directive
)
def update_directive(
    task_id: str,
    directive_id: str,
    body: UpdateDirective,
    container: Container = Depends(get_container),
) -> Directive:
    return container.directives.update(task_id, directive_id, body)


@router.delete(
    "/{task_id}/directives/{directive_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_directive(
    task_id: str,
    directive_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.directives.delete(task_id, directive_id)
