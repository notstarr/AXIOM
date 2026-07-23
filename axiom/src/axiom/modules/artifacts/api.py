from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from fastapi.responses import FileResponse

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.artifacts.schemas import Artifact, ArtifactContent


router = APIRouter(prefix="/tasks", tags=["artifacts"])


@router.get("/{task_id}/artifacts", response_model=list[Artifact])
def list_artifacts(
    task_id: str,
    container: Container = Depends(get_container),
) -> list[Artifact]:
    return container.artifacts.list(task_id)


@router.get("/{task_id}/artifacts/{artifact_id}/content", response_model=ArtifactContent)
def read_artifact_content(
    task_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> ArtifactContent:
    return container.artifacts.content(task_id, artifact_id)


@router.get("/{task_id}/artifacts/{artifact_id}/download", response_class=FileResponse)
def download_artifact(
    task_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> FileResponse:
    artifact, path = container.artifacts.path(task_id, artifact_id)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"X-Artifact-Id": artifact.id},
    )


@router.post(
    "/{task_id}/artifacts/upload",
    response_model=Artifact,
    status_code=status.HTTP_201_CREATED,
)
async def upload_artifact(
    task_id: str,
    file: UploadFile = File(...),
    actor: str = Form("human"),
    summary: str = Form(""),
    container: Container = Depends(get_container),
) -> Artifact:
    return container.artifacts.upload(
        task_id,
        filename=file.filename or "upload.bin",
        content=await file.read(),
        created_by=actor,
        summary=summary,
    )


@router.delete("/{task_id}/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_artifact(
    task_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> Response:
    container.artifacts.archive(task_id, artifact_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
