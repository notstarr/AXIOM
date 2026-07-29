from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from fastapi.responses import FileResponse

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.artifacts.schemas import Artifact, ArtifactContent


router = APIRouter(tags=["artifacts"])


@router.get("/tasks/{task_id}/artifacts", response_model=list[Artifact])
def list_artifacts(
    task_id: str,
    response: Response,
    search: str = "",
    artifact_type: str | None = None,
    phase: str | None = None,
    source: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[Artifact]:
    if limit is None:
        items = container.artifacts.list(task_id)
        response.headers["X-Total-Count"] = str(len(items))
        return items
    items, total = container.artifacts.list_page(
        task_id=task_id,
        search=search,
        artifact_type=artifact_type,
        phase=phase,
        source=source,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/artifacts", response_model=list[Artifact])
def list_all_artifacts(
    response: Response,
    task_id: str | None = None,
    project_id: str | None = None,
    search: str = "",
    artifact_type: str | None = None,
    phase: str | None = None,
    source: str | None = None,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> list[Artifact]:
    items, total = container.artifacts.list_page(
        task_id=task_id,
        project_id=project_id,
        search=search,
        artifact_type=artifact_type,
        phase=phase,
        source=source,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/content", response_model=ArtifactContent)
def read_artifact_content(
    task_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> ArtifactContent:
    return container.artifacts.content(task_id, artifact_id)


@router.get("/tasks/{task_id}/artifacts/{artifact_id}/download", response_class=FileResponse)
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
    "/tasks/{task_id}/artifacts/upload",
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


@router.delete("/tasks/{task_id}/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_artifact(
    task_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> Response:
    container.artifacts.archive(task_id, artifact_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
