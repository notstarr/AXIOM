from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.graph.schemas import Fact, Intent, TaskGraph, UpdateFact, UpdateIntent


router = APIRouter(prefix="/tasks", tags=["graph"])


@router.get("/{task_id}/graph", response_model=TaskGraph)
def get_graph(task_id: str, container: Container = Depends(get_container)) -> TaskGraph:
    return container.graph.get(task_id)


@router.get("/{task_id}/facts/{fact_id}", response_model=Fact)
def get_fact(
    task_id: str,
    fact_id: str,
    container: Container = Depends(get_container),
) -> Fact:
    return container.graph.fact(task_id, fact_id)


@router.patch("/{task_id}/facts/{fact_id}", response_model=Fact)
def update_fact(
    task_id: str,
    fact_id: str,
    body: UpdateFact,
    container: Container = Depends(get_container),
) -> Fact:
    return container.graph.update_fact(task_id, fact_id, body)


@router.delete("/{task_id}/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_fact(
    task_id: str,
    fact_id: str,
    container: Container = Depends(get_container),
) -> Response:
    container.graph.update_fact(task_id, fact_id, UpdateFact(archived=True))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/facts/{fact_id}/toggle-archive", response_model=Fact)
def toggle_fact_archive(
    task_id: str,
    fact_id: str,
    container: Container = Depends(get_container),
) -> Fact:
    current = container.graph.fact(task_id, fact_id)
    return container.graph.update_fact(task_id, fact_id, UpdateFact(archived=not current.archived))


@router.get("/{task_id}/intents/{intent_id}", response_model=Intent)
def get_intent(
    task_id: str,
    intent_id: str,
    container: Container = Depends(get_container),
) -> Intent:
    return container.graph.intent(task_id, intent_id)


@router.patch("/{task_id}/intents/{intent_id}", response_model=Intent)
def update_intent(
    task_id: str,
    intent_id: str,
    body: UpdateIntent,
    container: Container = Depends(get_container),
) -> Intent:
    return container.graph.update_intent(task_id, intent_id, body)


@router.delete("/{task_id}/intents/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_intent(
    task_id: str,
    intent_id: str,
    container: Container = Depends(get_container),
) -> Response:
    container.graph.update_intent(task_id, intent_id, UpdateIntent(archived=True))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/intents/{intent_id}/toggle-archive", response_model=Intent)
def toggle_intent_archive(
    task_id: str,
    intent_id: str,
    container: Container = Depends(get_container),
) -> Intent:
    current = container.graph.intent(task_id, intent_id)
    return container.graph.update_intent(
        task_id,
        intent_id,
        UpdateIntent(archived=not current.archived),
    )


@router.post("/{task_id}/intents/{intent_id}/reset-retries", response_model=Intent)
def reset_intent_retries(
    task_id: str,
    intent_id: str,
    container: Container = Depends(get_container),
) -> Intent:
    return container.graph.reset_intent_retries(task_id, intent_id)
