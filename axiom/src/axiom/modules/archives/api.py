from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.archives.schemas import (
    ArchiveBatch,
    ArchivePreview,
    ArchivePreviewRequest,
    ArchivedArtifactContent,
    CreateArchiveBatch,
    RestoreArchiveBatch,
)


router = APIRouter(prefix="/tasks/{task_id}/archives", tags=["archives"])


@router.post("/preview", response_model=ArchivePreview)
def preview_archive(
    task_id: str,
    body: ArchivePreviewRequest,
    container: Container = Depends(get_container),
) -> ArchivePreview:
    return container.archives.preview(task_id, body.root_type, body.root_id)


@router.post("", response_model=ArchiveBatch, status_code=201)
def create_archive(
    task_id: str,
    body: CreateArchiveBatch,
    container: Container = Depends(get_container),
) -> ArchiveBatch:
    return container.archives.create(
        task_id,
        root_type=body.root_type,
        root_id=body.root_id,
        expected_closure_hash=body.expected_closure_hash,
        reason=body.reason,
        actor=body.actor,
    )


@router.get("", response_model=list[ArchiveBatch])
def list_archives(
    task_id: str,
    status: str | None = None,
    container: Container = Depends(get_container),
) -> list[ArchiveBatch]:
    return container.archives.list(task_id, status=status)


@router.get("/{archive_id}", response_model=ArchiveBatch)
def get_archive(
    task_id: str,
    archive_id: str,
    container: Container = Depends(get_container),
) -> ArchiveBatch:
    return container.archives.get(task_id, archive_id)


@router.post("/{archive_id}/restore", response_model=ArchiveBatch)
def restore_archive(
    task_id: str,
    archive_id: str,
    body: RestoreArchiveBatch,
    container: Container = Depends(get_container),
) -> ArchiveBatch:
    return container.archives.restore(task_id, archive_id, actor=body.actor)


@router.get(
    "/{archive_id}/artifacts/{artifact_id}/content",
    response_model=ArchivedArtifactContent,
)
def archived_artifact_content(
    task_id: str,
    archive_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> ArchivedArtifactContent:
    return container.archives.artifact_content(task_id, archive_id, artifact_id)


@router.get("/{archive_id}/artifacts/{artifact_id}/download", response_class=FileResponse)
def download_archived_artifact(
    task_id: str,
    archive_id: str,
    artifact_id: str,
    container: Container = Depends(get_container),
) -> FileResponse:
    artifact, path = container.archives.artifact_path(task_id, archive_id, artifact_id)
    return FileResponse(
        path,
        filename=path.name,
        headers={"X-Artifact-Id": artifact.id, "X-Archive-Id": archive_id},
    )
