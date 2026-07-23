from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.reason.schemas import (
    AbandonWorkspace,
    CommitResult,
    DraftIntent,
    DraftTodo,
    DraftTodoUpdate,
    OpenWorkspace,
    ReasonWorkspace,
    SubmitWorkspace,
    WorkspaceWorkerCommand,
)


router = APIRouter(tags=["reason"])


@router.post("/tasks/{task_id}/reason/workspaces", response_model=ReasonWorkspace)
def open_workspace(
    task_id: str,
    body: OpenWorkspace,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.open(
        task_id=task_id,
        intent_id=body.intent_id,
        lease_id=body.lease_id,
        context_revision=body.context_revision,
    )


@router.get("/reason-workspaces/{workspace_id}", response_model=ReasonWorkspace)
def get_workspace(workspace_id: str, container: Container = Depends(get_container)) -> ReasonWorkspace:
    return container.reason.get(workspace_id)


@router.post("/reason-workspaces/{workspace_id}/abandon", response_model=ReasonWorkspace)
def abandon_workspace(
    workspace_id: str,
    body: AbandonWorkspace,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.abandon(
        workspace_id,
        lease_id=body.lease_id,
        failure_reason=body.failure_reason,
    )


@router.post("/reason-workspaces/{workspace_id}/todos", response_model=ReasonWorkspace)
def add_workspace_todo(
    workspace_id: str,
    body: DraftTodo,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.add_todo(workspace_id, body)


@router.post("/reason-workspaces/{workspace_id}/todo-updates", response_model=ReasonWorkspace)
def update_workspace_todo(
    workspace_id: str,
    body: DraftTodoUpdate,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.update_todo(workspace_id, body)


@router.post("/reason-workspaces/{workspace_id}/intents", response_model=ReasonWorkspace)
def propose_workspace_intent(
    workspace_id: str,
    body: DraftIntent,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.propose_intent(workspace_id, body)


@router.post("/reason-workspaces/{workspace_id}/commit", response_model=CommitResult)
def commit_workspace(
    workspace_id: str,
    body: WorkspaceWorkerCommand,
    container: Container = Depends(get_container),
) -> CommitResult:
    return container.reason.commit(workspace_id, lease_id=body.lease_id)


@router.post("/reason-workspaces/{workspace_id}/submit", response_model=CommitResult)
def submit_workspace(
    workspace_id: str,
    body: SubmitWorkspace,
    container: Container = Depends(get_container),
) -> CommitResult:
    return container.reason.submit(workspace_id, body.draft, lease_id=body.lease_id)
