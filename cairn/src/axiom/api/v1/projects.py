from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_project_service
from axiom.modules.projects.schemas import CreateProject, Project
from axiom.modules.projects.service import ProjectService


router = APIRouter(prefix="/projects", tags=["v1-projects"])


@router.post("", response_model=Project, status_code=201)
def create_project(
    body: CreateProject,
    service: ProjectService = Depends(get_project_service),
) -> Project:
    return service.create(body)


@router.get("", response_model=list[Project])
def list_projects(service: ProjectService = Depends(get_project_service)) -> list[Project]:
    return service.list()


@router.get("/{project_id}", response_model=Project)
def get_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> Project:
    project = service.get(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project
