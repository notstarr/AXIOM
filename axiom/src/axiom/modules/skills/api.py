from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.skills.schemas import (
    CreateSkill,
    SaveSkill,
    SetSkillEnabled,
    Skill,
    SkillContent,
)


router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[Skill])
def list_skills(container: Container = Depends(get_container)) -> list[Skill]:
    return container.skills.list()


@router.get("/usage")
def skill_usage(container: Container = Depends(get_container)) -> dict:
    return container.skills.usage()


@router.post("", response_model=SkillContent, status_code=status.HTTP_201_CREATED)
def create_skill(body: CreateSkill, container: Container = Depends(get_container)) -> SkillContent:
    return container.skills.create(body)


@router.get("/directories", response_model=list[str])
def skill_directories(container: Container = Depends(get_container)) -> list[str]:
    return container.skills.directories()


@router.get("/{name}", response_model=SkillContent)
def get_skill(name: str, container: Container = Depends(get_container)) -> SkillContent:
    return container.skills.get(name)


@router.get("/{name}/files")
def list_skill_files(name: str, container: Container = Depends(get_container)) -> list[dict[str, object]]:
    return container.skills.files(name)


@router.get("/{name}/files/{path:path}")
def get_skill_file(name: str, path: str, container: Container = Depends(get_container)) -> dict[str, str]:
    return container.skills.file(name, path)


@router.post("/{name}/files")
def create_skill_file(name: str, body: dict[str, str], container: Container = Depends(get_container)) -> dict[str, str]:
    return container.skills.create_file(name, body.get("path", ""), body.get("content", ""))


@router.put("/{name}/files/{path:path}")
def save_skill_file(name: str, path: str, body: dict[str, str], container: Container = Depends(get_container)) -> dict[str, str]:
    return container.skills.save_file(name, path, body.get("content", ""))


@router.delete("/{name}/files/{path:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill_file(name: str, path: str, container: Container = Depends(get_container)) -> Response:
    container.skills.delete_file(name, path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{name}", response_model=SkillContent)
def save_skill(
    name: str,
    body: SaveSkill,
    container: Container = Depends(get_container),
) -> SkillContent:
    return container.skills.save(name, body)


@router.patch("/{name}/enabled", response_model=SkillContent)
def set_skill_enabled(
    name: str,
    body: SetSkillEnabled,
    container: Container = Depends(get_container),
) -> SkillContent:
    return container.skills.set_enabled(name, body)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(name: str, container: Container = Depends(get_container)) -> Response:
    container.skills.delete(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
