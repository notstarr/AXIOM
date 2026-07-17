from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.artifacts.schemas import Artifact, RegisterArtifact
from axiom.modules.artifacts.service import ArtifactService


router = APIRouter(tags=["v1-artifacts"])


def get_service(request: Request, engine=Depends(get_v2_engine)) -> ArtifactService:
    return ArtifactService(engine, data_dir=getattr(request.app.state, "data_dir", None))


@router.get("/tasks/{task_id}/artifacts", response_model=list[Artifact])
def list_artifacts(
    task_id: str,
    service: ArtifactService = Depends(get_service),
) -> list[Artifact]:
    return service.list(task_id)


@router.post(
    "/internal/tasks/{task_id}/artifacts",
    response_model=Artifact,
    status_code=201,
    dependencies=[Depends(require_service_token)],
)
def register_artifact(
    task_id: str,
    body: RegisterArtifact,
    service: ArtifactService = Depends(get_service),
) -> Artifact:
    try:
        return service.register(task_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 422, detail) from exc
