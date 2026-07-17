from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.v1.dependencies import get_v2_engine
from axiom.modules.graph.schemas import Intent
from axiom.modules.graph.service import IntentService


router = APIRouter(tags=["v1-graph"])


@router.get("/tasks/{task_id}/intents", response_model=list[Intent])
def list_intents(task_id: str, engine=Depends(get_v2_engine)) -> list[Intent]:
    return IntentService(engine).list(task_id)
