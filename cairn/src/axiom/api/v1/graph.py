from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from axiom.api.v1.dependencies import get_v2_engine
from axiom.infrastructure.database.metadata import facts
from axiom.modules.graph.schemas import Fact, Intent
from axiom.modules.graph.service import IntentService


router = APIRouter(tags=["v1-graph"])


class TaskGraph(BaseModel):
    facts: list[Fact]
    intents: list[Intent]


@router.get("/tasks/{task_id}/intents", response_model=list[Intent])
def list_intents(task_id: str, engine=Depends(get_v2_engine)) -> list[Intent]:
    return IntentService(engine).list(task_id)


@router.get("/tasks/{task_id}/graph", response_model=TaskGraph)
def get_task_graph(task_id: str, engine=Depends(get_v2_engine)) -> TaskGraph:
    with engine.connect() as conn:
        fact_rows = conn.execute(
            select(facts)
            .where(facts.c.task_id == task_id, facts.c.archived.is_(False))
            .order_by(facts.c.created_at, facts.c.id)
        ).mappings()
        fact_models = [Fact(**row) for row in fact_rows]
    return TaskGraph(facts=fact_models, intents=IntentService(engine).list(task_id))
