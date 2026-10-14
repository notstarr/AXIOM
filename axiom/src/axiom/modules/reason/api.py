from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.reason.schemas import (
    AbandonWorkspace,
    AddWorkspaceChain,
    AddWorkspaceTodo,
    AssessWorkspaceGoal,
    AssessWorkspaceIntent,
    CommitResult,
    DeleteWorkspaceTodo,
    LinkWorkspaceChainIntent,
    OpenWorkspace,
    ProposeWorkspaceIntent,
    ReasonWorkspace,
    UpdateWorkspaceChain,
    UpdateWorkspaceTodo,
    WorkspaceStatus,
    WorkspaceWorkerCommand,
    ReasonRun,
    ReasonReconcileReport,
    ReleasedReasonRun,
    StartedReasonRun,
    TriggerReasonRun,
)
from axiom.modules.execution.schemas import ActivityCommand, ExecutionInput, LeaseCommand, ReleaseIntentCommand


router = APIRouter(tags=["reason"])


@router.post("/dispatch/reason-runs/reconcile", response_model=ReasonReconcileReport)
def reconcile_reason_runs(
    container: Container = Depends(get_container),
) -> ReasonReconcileReport:
    return container.reason.reconcile_all()


@router.post("/tasks/{task_id}/reason-runs/trigger", response_model=ReasonRun)
def trigger_reason_run(
    task_id: str,
    body: TriggerReasonRun,
    container: Container = Depends(get_container),
) -> ReasonRun:
    return container.reason.trigger_manual(task_id, actor=body.actor)


@router.post("/tasks/{task_id}/reason/workspaces", response_model=ReasonWorkspace)
def open_workspace(
    task_id: str,
    body: OpenWorkspace,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.open(
        task_id=task_id,
        reason_run_id=body.reason_run_id,
        lease_id=body.lease_id,
        context_revision=body.context_revision,
    )


@router.post("/tasks/{task_id}/reason-runs/{reason_run_id}/reset-retries", response_model=ReasonRun)
def reset_reason_run_retries(
    task_id: str,
    reason_run_id: str,
    container: Container = Depends(get_container),
) -> ReasonRun:
    """Release a ReasonRun parked by an exhausted infrastructure retry budget."""

    return container.reason.reset_run_retries(task_id, reason_run_id)


@router.get("/tasks/{task_id}/reason-runs/{reason_run_id}", response_model=ReasonRun)
def get_reason_run(
    task_id: str,
    reason_run_id: str,
    container: Container = Depends(get_container),
) -> ReasonRun:
    return container.reason.get_run(task_id, reason_run_id)


@router.post(
    "/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}/start",
    response_model=StartedReasonRun,
)
def start_reason_run(
    task_id: str,
    reason_run_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> StartedReasonRun:
    return container.reason.start_run(task_id, reason_run_id, lease_id=body.lease_id)


@router.post(
    "/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}/input",
    response_model=ExecutionInput,
)
def get_reason_run_input(
    task_id: str,
    reason_run_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> ExecutionInput:
    return container.execution.reason_input(
        task_id=task_id,
        reason_run_id=reason_run_id,
        command=body,
    )


@router.post(
    "/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}/heartbeat",
    status_code=204,
)
def heartbeat_reason_run(
    task_id: str,
    reason_run_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> None:
    container.reason.heartbeat_run(task_id, reason_run_id, lease_id=body.lease_id)


@router.post(
    "/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}/activity",
    status_code=204,
)
def update_reason_run_activity(
    task_id: str,
    reason_run_id: str,
    body: ActivityCommand,
    container: Container = Depends(get_container),
) -> None:
    container.reason.activity_run(
        task_id,
        reason_run_id,
        lease_id=body.lease_id,
        activity_kind=body.activity_kind,
        activity_summary=body.activity_summary,
        activity_tool=body.activity_tool,
    )


@router.post(
    "/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}/conclude",
    status_code=204,
)
def conclude_reason_run(
    task_id: str,
    reason_run_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> None:
    container.reason.begin_conclude_run(task_id, reason_run_id, lease_id=body.lease_id)


@router.post(
    "/dispatch/tasks/{task_id}/reason-runs/{reason_run_id}/release",
    response_model=ReleasedReasonRun,
)
def release_reason_run(
    task_id: str,
    reason_run_id: str,
    body: ReleaseIntentCommand,
    container: Container = Depends(get_container),
) -> ReleasedReasonRun:
    return container.reason.release_run(
        task_id,
        reason_run_id,
        lease_id=body.lease_id,
        failure_kind=body.failure_kind,
        failure_class=body.failure_class,
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
    body: AddWorkspaceTodo,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.add_todo(workspace_id, body.todo, lease_id=body.lease_id)


@router.post("/reason-workspaces/{workspace_id}/todo-updates", response_model=ReasonWorkspace)
def update_workspace_todo(
    workspace_id: str,
    body: UpdateWorkspaceTodo,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.update_todo(
        workspace_id,
        body.update,
        lease_id=body.lease_id,
    )


@router.post("/reason-workspaces/{workspace_id}/todo-deletions", response_model=ReasonWorkspace)
def delete_workspace_todo(
    workspace_id: str,
    body: DeleteWorkspaceTodo,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.delete_todo(
        workspace_id,
        body.deletion,
        lease_id=body.lease_id,
    )


@router.post("/reason-workspaces/{workspace_id}/intents", response_model=ReasonWorkspace)
def propose_workspace_intent(
    workspace_id: str,
    body: ProposeWorkspaceIntent,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.propose_intent(
        workspace_id,
        body.intent,
        lease_id=body.lease_id,
    )


@router.post(
    "/reason-workspaces/{workspace_id}/intent-assessments",
    response_model=ReasonWorkspace,
)
@router.post(
    "/reason-workspaces/{workspace_id}/intent-assessment",
    response_model=ReasonWorkspace,
    include_in_schema=False,
)
@router.post(
    "/reason-workspaces/{workspace_id}/intents/assess",
    response_model=ReasonWorkspace,
    include_in_schema=False,
)
def assess_workspace_intent(
    workspace_id: str,
    body: AssessWorkspaceIntent,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.assess_intent(
        workspace_id,
        body.assessment,
        lease_id=body.lease_id,
    )


@router.post("/reason-workspaces/{workspace_id}/goal-assessment", response_model=ReasonWorkspace)
def assess_workspace_goal(
    workspace_id: str,
    body: AssessWorkspaceGoal,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.assess_goal(
        workspace_id,
        body.assessment,
        lease_id=body.lease_id,
    )


@router.post("/reason-workspaces/{workspace_id}/chains", response_model=ReasonWorkspace)
def add_workspace_chain(
    workspace_id: str,
    body: AddWorkspaceChain,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.add_chain(workspace_id, body.chain, lease_id=body.lease_id)


@router.post("/reason-workspaces/{workspace_id}/chain-updates", response_model=ReasonWorkspace)
def update_workspace_chain(
    workspace_id: str,
    body: UpdateWorkspaceChain,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.update_chain(
        workspace_id,
        body.update,
        lease_id=body.lease_id,
    )


@router.post("/reason-workspaces/{workspace_id}/chain-links", response_model=ReasonWorkspace)
def link_workspace_chain_intent(
    workspace_id: str,
    body: LinkWorkspaceChainIntent,
    container: Container = Depends(get_container),
) -> ReasonWorkspace:
    return container.reason.link_chain_intent(
        workspace_id,
        body.link,
        lease_id=body.lease_id,
    )


@router.post("/reason-workspaces/{workspace_id}/status", response_model=WorkspaceStatus)
def workspace_status(
    workspace_id: str,
    body: WorkspaceWorkerCommand,
    container: Container = Depends(get_container),
) -> WorkspaceStatus:
    return container.reason.status(workspace_id, lease_id=body.lease_id)


@router.post("/reason-workspaces/{workspace_id}/commit", response_model=CommitResult)
def commit_workspace(
    workspace_id: str,
    body: WorkspaceWorkerCommand,
    container: Container = Depends(get_container),
) -> CommitResult:
    return container.reason.commit(workspace_id, lease_id=body.lease_id)
