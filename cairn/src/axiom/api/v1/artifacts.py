from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

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


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/content", response_class=FileResponse)
def read_artifact_content(
    task_id: str,
    artifact_id: str,
    service: ArtifactService = Depends(get_service),
) -> FileResponse:
    try:
        path = service.content_path(task_id, artifact_id)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc
    return FileResponse(path, filename=None)


@router.post("/tasks/{task_id}/artifacts/upload", response_model=Artifact, status_code=201)
def upload_artifact(
    task_id: str,
    file: UploadFile = File(...),
    actor: str = Form("human"),
    service: ArtifactService = Depends(get_service),
) -> Artifact:
    try:
        return service.upload(task_id, filename=file.filename or "upload.bin", stream=file.file, actor=actor)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.delete("/tasks/{task_id}/artifacts/{artifact_id}", status_code=204)
def delete_artifact(
    task_id: str,
    artifact_id: str,
    service: ArtifactService = Depends(get_service),
) -> None:
    try:
        service.archive(task_id, artifact_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


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
