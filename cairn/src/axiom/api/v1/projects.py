from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_project_service
from axiom.modules.projects.schemas import CreateProject, Project, UpdateProject
from axiom.modules.projects.service import ProjectService


router = APIRouter(prefix="/projects", tags=["v1-projects"])


@router.post("", response_model=Project, status_code=201)
def create_project(
    body: CreateProject,
    service: ProjectService = Depends(get_project_service),
) -> Project:
    return service.create(body)


@router.get("", response_model=list[Project])
def list_projects(
    search: str = "",
    status: str | None = None,
    service: ProjectService = Depends(get_project_service),
) -> list[Project]:
    return service.list(search=search, status=status)


@router.get("/{project_id}", response_model=Project)
def get_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> Project:
    project = service.get(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


@router.patch("/{project_id}", response_model=Project)
def update_project(
    project_id: str,
    body: UpdateProject,
    service: ProjectService = Depends(get_project_service),
) -> Project:
    try:
        return service.update(project_id, body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> None:
    try:
        service.delete(project_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
