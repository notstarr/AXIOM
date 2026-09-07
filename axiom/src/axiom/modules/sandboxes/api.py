from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.sandboxes.schemas import TaskSandbox
from axiom.modules.settings.service import load_runtime_config


router = APIRouter(prefix="/tasks", tags=["sandboxes"])


def _sandbox_file_limit(container: Container) -> int:
    with container.engine.connect() as conn:
        return int(load_runtime_config(conn).browser_file_bridge_limit)


class SandboxFileRequest(BaseModel):
    owner_id: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=2_000)
    content_base64: str = ""


@router.get("/{task_id}/sandbox", response_model=TaskSandbox | None)
def get_task_sandbox(task_id: str, container: Container = Depends(get_container)) -> TaskSandbox | None:
    return container.sandboxes.get(task_id)


@router.post("/{task_id}/sandbox-runs/{lease_id}/read")
def read_sandbox_run_file(
    task_id: str,
    lease_id: str,
    payload: SandboxFileRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    try:
        content = container.sandboxes.file_read(
            task_id=task_id,
            owner_id=payload.owner_id,
            lease_id=lease_id,
            path=payload.path,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="sandbox file not found") from exc
    if len(content) > _sandbox_file_limit(container):
        raise HTTPException(status_code=413, detail="sandbox file is too large")
    return {"content_base64": base64.b64encode(content).decode("ascii"), "size": len(content)}


@router.post("/{task_id}/sandbox-runs/{lease_id}/write")
def write_sandbox_run_file(
    task_id: str,
    lease_id: str,
    payload: SandboxFileRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    try:
        content = base64.b64decode(payload.content_base64.encode("ascii"), validate=True)
        if len(content) > _sandbox_file_limit(container):
            raise HTTPException(status_code=413, detail="sandbox file is too large")
        container.sandboxes.file_write(
            task_id=task_id,
            owner_id=payload.owner_id,
            lease_id=lease_id,
            path=payload.path,
            content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid base64 content") from exc
    return {"ok": True, "size": len(content)}


@router.post("/{task_id}/sandbox-runs/{lease_id}/append")
def append_sandbox_run_file(
    task_id: str,
    lease_id: str,
    payload: SandboxFileRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    try:
        content = base64.b64decode(payload.content_base64.encode("ascii"), validate=True)
        if len(content) > _sandbox_file_limit(container):
            raise HTTPException(status_code=413, detail="sandbox file is too large")
        container.sandboxes.file_write(
            task_id=task_id,
            owner_id=payload.owner_id,
            lease_id=lease_id,
            path=payload.path,
            content=content,
            append=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid base64 content") from exc
    return {"ok": True, "size": len(content)}


@router.post("/{task_id}/sandbox-runs/{lease_id}/exists")
def sandbox_run_file_exists(
    task_id: str,
    lease_id: str,
    payload: SandboxFileRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    return {
        "exists": container.sandboxes.file_exists(
            task_id=task_id,
            owner_id=payload.owner_id,
            lease_id=lease_id,
            path=payload.path,
        )
    }
