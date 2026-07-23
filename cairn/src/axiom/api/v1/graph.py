from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from axiom.api.v1.dependencies import get_v2_engine
from axiom.infrastructure.database.metadata import facts
from axiom.modules.graph.schemas import Fact, Intent
from axiom.modules.graph.service import GraphManagementService, IntentService


router = APIRouter(tags=["v1-graph"])


class TaskGraph(BaseModel):
    facts: list[Fact]
    intents: list[Intent]


class UpdateGraphDescription(BaseModel):
    description: str
    actor: str = "human.frontend"


class GraphMutation(BaseModel):
    actor: str = "human.frontend"


def _raise_graph_error(exc: ValueError) -> None:
    detail = str(exc)
    if "not found:" in detail.lower():
        raise HTTPException(404, detail) from exc
    raise HTTPException(409 if "cannot" in detail.lower() or "not running" in detail.lower() or "read-only" in detail.lower() else 422, detail) from exc


@router.get("/tasks/{task_id}/intents", response_model=list[Intent])
def list_intents(
    task_id: str,
    include_archived: bool = False,
    engine=Depends(get_v2_engine),
) -> list[Intent]:
    return IntentService(engine).list(task_id, include_archived=include_archived)


@router.get("/tasks/{task_id}/intents/{intent_id}", response_model=Intent)
def get_intent(task_id: str, intent_id: str, engine=Depends(get_v2_engine)) -> Intent:
    try:
        return IntentService(engine).get(task_id, intent_id)
    except ValueError as exc:
        _raise_graph_error(exc)


@router.get("/tasks/{task_id}/graph", response_model=TaskGraph)
def get_task_graph(task_id: str, engine=Depends(get_v2_engine)) -> TaskGraph:
    with engine.connect() as conn:
        fact_rows = conn.execute(
            select(facts)
            .where(facts.c.task_id == task_id)
            .order_by(facts.c.created_at, facts.c.id)
        ).mappings()
        fact_models = [Fact(**row) for row in fact_rows]
    return TaskGraph(
        facts=fact_models,
        intents=IntentService(engine).list(task_id, include_archived=True),
    )


@router.patch("/tasks/{task_id}/facts/{fact_id}", response_model=Fact)
def update_fact(task_id: str, fact_id: str, body: UpdateGraphDescription, engine=Depends(get_v2_engine)) -> Fact:
    try:
        return GraphManagementService(engine).update_fact(
            task_id, fact_id, description=body.description, actor=body.actor
        )
    except ValueError as exc:
        _raise_graph_error(exc)


@router.post("/tasks/{task_id}/facts/{fact_id}/toggle-archive", response_model=Fact)
def toggle_fact_archive(task_id: str, fact_id: str, body: GraphMutation, engine=Depends(get_v2_engine)) -> Fact:
    try:
        return GraphManagementService(engine).toggle_fact_archive(task_id, fact_id, actor=body.actor)
    except ValueError as exc:
        _raise_graph_error(exc)


@router.delete("/tasks/{task_id}/facts/{fact_id}", status_code=204)
def delete_fact(task_id: str, fact_id: str, actor: str = "human.frontend", engine=Depends(get_v2_engine)) -> None:
    try:
        GraphManagementService(engine).delete_fact(task_id, fact_id, actor=actor)
    except ValueError as exc:
        _raise_graph_error(exc)


@router.patch("/tasks/{task_id}/intents/{intent_id}", response_model=Intent)
def update_intent(task_id: str, intent_id: str, body: UpdateGraphDescription, engine=Depends(get_v2_engine)) -> Intent:
    try:
        return GraphManagementService(engine).update_intent(
            task_id, intent_id, description=body.description, actor=body.actor
        )
    except ValueError as exc:
        _raise_graph_error(exc)


@router.post("/tasks/{task_id}/intents/{intent_id}/toggle-archive", response_model=Intent)
def toggle_intent_archive(task_id: str, intent_id: str, body: GraphMutation, engine=Depends(get_v2_engine)) -> Intent:
    try:
        return GraphManagementService(engine).toggle_intent_archive(task_id, intent_id, actor=body.actor)
    except ValueError as exc:
        _raise_graph_error(exc)


@router.delete("/tasks/{task_id}/intents/{intent_id}", status_code=204)
def delete_intent(task_id: str, intent_id: str, actor: str = "human.frontend", engine=Depends(get_v2_engine)) -> None:
    try:
        GraphManagementService(engine).delete_intent(task_id, intent_id, actor=actor)
    except ValueError as exc:
        _raise_graph_error(exc)
