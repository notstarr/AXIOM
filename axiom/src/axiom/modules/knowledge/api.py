from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.knowledge.schemas import CreateKnowledge, Knowledge, UpdateKnowledge


router = APIRouter(prefix="/projects", tags=["knowledge"])


@router.get("/{project_id}/knowledge", response_model=list[Knowledge])
def list_knowledge(
    project_id: str,
    include_archived: bool = False,
    container: Container = Depends(get_container),
) -> list[Knowledge]:
    return container.knowledge.list(project_id, include_archived=include_archived)


@router.post(
    "/{project_id}/knowledge",
    response_model=Knowledge,
    status_code=status.HTTP_201_CREATED,
)
def create_knowledge(
    project_id: str,
    body: CreateKnowledge,
    container: Container = Depends(get_container),
) -> Knowledge:
    return container.knowledge.create(project_id, body)


@router.patch("/{project_id}/knowledge/{knowledge_id}", response_model=Knowledge)
def update_knowledge(
    project_id: str,
    knowledge_id: str,
    body: UpdateKnowledge,
    container: Container = Depends(get_container),
) -> Knowledge:
    return container.knowledge.update(project_id, knowledge_id, body)


@router.delete("/{project_id}/knowledge/{knowledge_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge(
    project_id: str,
    knowledge_id: str,
    container: Container = Depends(get_container),
) -> Response:
    container.knowledge.delete(project_id, knowledge_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
