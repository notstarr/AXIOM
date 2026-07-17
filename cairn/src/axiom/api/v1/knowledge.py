from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_v2_engine
from axiom.modules.knowledge.schemas import CreateKnowledge, CreateTaskHint, Knowledge, TaskHint
from axiom.modules.knowledge.service import KnowledgeService


router = APIRouter(tags=["v1-knowledge-hints"])


def get_service(engine=Depends(get_v2_engine)) -> KnowledgeService:
    return KnowledgeService(engine)


@router.get("/projects/{project_id}/knowledge", response_model=list[Knowledge])
def list_project_knowledge(
    project_id: str, service: KnowledgeService = Depends(get_service)
) -> list[Knowledge]:
    return service.list_project_knowledge(project_id)


@router.post("/projects/{project_id}/knowledge", response_model=Knowledge, status_code=201)
def add_project_knowledge(
    project_id: str,
    body: CreateKnowledge,
    service: KnowledgeService = Depends(get_service),
) -> Knowledge:
    try:
        return service.add_project_knowledge(project_id, body)
    except ValueError as exc:
        raise HTTPException(404 if "not found:" in str(exc).lower() else 409, str(exc)) from exc


@router.get("/tasks/{task_id}/hints", response_model=list[TaskHint])
def list_task_hints(task_id: str, service: KnowledgeService = Depends(get_service)) -> list[TaskHint]:
    return service.list_task_hints(task_id)


@router.post("/tasks/{task_id}/hints", response_model=TaskHint, status_code=201)
def add_task_hint(
    task_id: str,
    body: CreateTaskHint,
    service: KnowledgeService = Depends(get_service),
) -> TaskHint:
    try:
        return service.add_task_hint(task_id, body)
    except ValueError as exc:
        raise HTTPException(404 if "not found:" in str(exc).lower() else 409, str(exc)) from exc
