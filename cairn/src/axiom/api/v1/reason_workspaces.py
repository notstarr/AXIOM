from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.reason_workspace.schemas import (
    CommitResult,
    DraftIntent,
    DraftTodo,
    DraftTodoUpdate,
    ReasonWorkspace,
)
from axiom.modules.reason_workspace.service import ReasonWorkspaceService


router = APIRouter(
    tags=["v1-reason-workspaces"],
    dependencies=[Depends(require_service_token)],
)


class OpenWorkspaceRequest(BaseModel):
    worker: str
    context_revision: int = Field(ge=0)

    @field_validator("worker")
    @classmethod
    def require_worker(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("worker must not be empty")
        return text


class AbandonWorkspaceRequest(BaseModel):
    worker: str
    failure_reason: str


def get_workspace_service(engine=Depends(get_v2_engine)) -> ReasonWorkspaceService:
    return ReasonWorkspaceService(engine)


def _http_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    lowered = detail.lower()
    if "not found:" in lowered:
        return HTTPException(404, detail)
    if "already committed" in lowered or "not open" in lowered or "not writable" in lowered:
        return HTTPException(409, detail)
    return HTTPException(422, detail)


@router.post("/tasks/{task_id}/reason/workspaces", response_model=ReasonWorkspace, status_code=201)
def open_workspace(
    task_id: str,
    body: OpenWorkspaceRequest,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> ReasonWorkspace:
    try:
        return service.open(task_id, worker=body.worker, context_revision=body.context_revision)
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.get("/reason-workspaces/{workspace_id}", response_model=ReasonWorkspace)
def get_workspace(
    workspace_id: str,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> ReasonWorkspace:
    try:
        return service.get(workspace_id)
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.post("/reason-workspaces/{workspace_id}/abandon", response_model=ReasonWorkspace)
def abandon_workspace(
    workspace_id: str,
    body: AbandonWorkspaceRequest,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> ReasonWorkspace:
    try:
        return service.abandon(
            workspace_id,
            worker=body.worker,
            failure_reason=body.failure_reason,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.post("/reason-workspaces/{workspace_id}/todos", response_model=ReasonWorkspace)
def add_todo(
    workspace_id: str,
    body: DraftTodo,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> ReasonWorkspace:
    try:
        return service.add_todo(workspace_id, body)
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.post("/reason-workspaces/{workspace_id}/todo-updates", response_model=ReasonWorkspace)
def update_todo(
    workspace_id: str,
    body: DraftTodoUpdate,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> ReasonWorkspace:
    try:
        return service.update_todo(workspace_id, body)
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.post("/reason-workspaces/{workspace_id}/intents", response_model=ReasonWorkspace)
def propose_intent(
    workspace_id: str,
    body: DraftIntent,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> ReasonWorkspace:
    try:
        return service.propose_intent(workspace_id, body)
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.post("/reason-workspaces/{workspace_id}/commit", response_model=CommitResult)
def commit_workspace(
    workspace_id: str,
    service: ReasonWorkspaceService = Depends(get_workspace_service),
) -> CommitResult:
    try:
        return service.commit(workspace_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
