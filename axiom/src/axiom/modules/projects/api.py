from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.projects.schemas import CreateProject, Project, UpdateProject


router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(body: CreateProject, container: Container = Depends(get_container)) -> Project:
    return container.projects.create(body)


@router.get("", response_model=list[Project])
def list_projects(
    search: str = "",
    status: str | None = None,
    container: Container = Depends(get_container),
) -> list[Project]:
    return container.projects.list(search=search, status=status)


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: str, container: Container = Depends(get_container)) -> Project:
    return container.projects.get(project_id)


@router.patch("/{project_id}", response_model=Project)
def update_project(
    project_id: str,
    body: UpdateProject,
    container: Container = Depends(get_container),
) -> Project:
    return container.projects.update(project_id, body)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, container: Container = Depends(get_container)) -> Response:
    container.projects.delete(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
