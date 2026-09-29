from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.graph.schemas import (
    Fact,
    GraphChildren,
    GraphCounts,
    Intent,
    TaskGraph,
    UpdateFact,
    UpdateIntent,
)


router = APIRouter(prefix="/tasks", tags=["graph"])


@router.get("/{task_id}/graph", response_model=TaskGraph)
def get_graph(
    task_id: str,
    scope: str = "full",
    container: Container = Depends(get_container),
) -> TaskGraph:
    """scope=skeleton 只回 origin/goal 骨架，其余节点走 /graph/children 按需展开。"""
    return container.graph.get(task_id, scope=scope)


@router.get("/{task_id}/graph/counts", response_model=GraphCounts)
def get_graph_counts(task_id: str, container: Container = Depends(get_container)) -> GraphCounts:
    return container.graph.counts(task_id)


@router.get("/{task_id}/graph/children", response_model=GraphChildren)
def get_graph_children(
    task_id: str,
    node: str,
    limit: int = 100,
    container: Container = Depends(get_container),
) -> GraphChildren:
    return container.graph.children(task_id, node, limit=limit)


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
