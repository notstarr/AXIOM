from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.execution.schemas import (
    ActivityCommand,
    LeaseCommand,
    PlanExecutionInput,
    ReleaseIntentCommand,
)
from axiom.modules.plan.schemas import (
    AbandonPlanWorkspace,
    AddPlanChain,
    AddPlanTodo,
    DeletePlanTodo,
    LinkPlanChainIntent,
    PlanCommitResult,
    PlanReconcileReport,
    PlanRun,
    PlanWorkspace,
    OpenPlanWorkspace,
    PlanWorkspaceCommand,
    PlanWorkspaceStatus,
    ProposePlanIntent,
    ReleasedPlanRun,
    StartedPlanRun,
    SubmitPlanDecision,
    TriggerPlanRun,
    UpdatePlanChain,
    UpdatePlanTodo,
)


router = APIRouter(tags=["plan"])


@router.post("/dispatch/plan-runs/reconcile", response_model=PlanReconcileReport)
def reconcile_plan_runs(container: Container = Depends(get_container)) -> PlanReconcileReport:
    return container.plan.reconcile_all()


@router.post("/tasks/{task_id}/plan-runs/trigger", response_model=PlanRun)
def trigger_plan_run(task_id: str, body: TriggerPlanRun, container: Container = Depends(get_container)) -> PlanRun:
    return container.plan.trigger(task_id, actor=body.actor, trigger=body.trigger, trigger_payload=body.trigger_payload)


@router.get("/tasks/{task_id}/plan-runs/{plan_run_id}", response_model=PlanRun)
def get_plan_run(task_id: str, plan_run_id: str, container: Container = Depends(get_container)) -> PlanRun:
    return container.plan.get_run(task_id, plan_run_id)


@router.post("/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}/start", response_model=StartedPlanRun)
def start_plan_run(task_id: str, plan_run_id: str, body: LeaseCommand, container: Container = Depends(get_container)) -> StartedPlanRun:
    return container.plan.start_run(task_id, plan_run_id, lease_id=body.lease_id)


@router.post("/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}/heartbeat", status_code=204)
def heartbeat_plan_run(task_id: str, plan_run_id: str, body: LeaseCommand, container: Container = Depends(get_container)) -> None:
    container.plan.heartbeat_run(task_id, plan_run_id, lease_id=body.lease_id)


@router.post(
    "/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}/input",
    response_model=PlanExecutionInput,
)
def input_plan_run(
    task_id: str,
    plan_run_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> PlanExecutionInput:
    return container.execution.plan_input(
        task_id=task_id,
        plan_run_id=plan_run_id,
        command=body,
    )


@router.post("/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}/activity", status_code=204)
def activity_plan_run(task_id: str, plan_run_id: str, body: ActivityCommand, container: Container = Depends(get_container)) -> None:
    container.plan.activity_run(task_id, plan_run_id, lease_id=body.lease_id, activity_kind=body.activity_kind, activity_summary=body.activity_summary, activity_tool=body.activity_tool)


@router.post("/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}/conclude", status_code=204)
def conclude_plan_run(task_id: str, plan_run_id: str, body: LeaseCommand, container: Container = Depends(get_container)) -> None:
    container.plan.begin_conclude_run(task_id, plan_run_id, lease_id=body.lease_id)


@router.post("/dispatch/tasks/{task_id}/plan-runs/{plan_run_id}/release", response_model=ReleasedPlanRun)
def release_plan_run(task_id: str, plan_run_id: str, body: ReleaseIntentCommand, container: Container = Depends(get_container)) -> ReleasedPlanRun:
    return container.plan.release_run(task_id, plan_run_id, lease_id=body.lease_id, failure_kind=body.failure_kind, failure_class=body.failure_class)


@router.post("/tasks/{task_id}/plan/workspaces", response_model=PlanWorkspace)
def open_plan_workspace(task_id: str, body: OpenPlanWorkspace, container: Container = Depends(get_container)) -> PlanWorkspace:
    # Worker gets the context revision from the Plan input in normal runtime;
    # API callers may pass it in the query for a minimal local protocol.
    return container.plan.open(task_id=task_id, plan_run_id=body.plan_run_id, lease_id=body.lease_id, context_revision=body.context_revision)


@router.get("/plan-workspaces/{workspace_id}", response_model=PlanWorkspace)
def get_plan_workspace(workspace_id: str, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.get(workspace_id)


@router.post("/plan-workspaces/{workspace_id}/abandon", response_model=PlanWorkspace)
def abandon_plan_workspace(workspace_id: str, body: AbandonPlanWorkspace, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.abandon(workspace_id, lease_id=body.lease_id, failure_reason=body.failure_reason)


@router.post("/plan-workspaces/{workspace_id}/todos", response_model=PlanWorkspace)
def add_plan_todo(workspace_id: str, body: AddPlanTodo, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.add_todo(workspace_id, body.todo, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/todo-updates", response_model=PlanWorkspace)
def update_plan_todo(workspace_id: str, body: UpdatePlanTodo, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.update_todo(workspace_id, body.update, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/todo-deletions", response_model=PlanWorkspace)
def delete_plan_todo(workspace_id: str, body: DeletePlanTodo, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.delete_todo(workspace_id, body.deletion, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/intents", response_model=PlanWorkspace)
def propose_plan_intent(workspace_id: str, body: ProposePlanIntent, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.propose_intent(workspace_id, body.intent, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/chains", response_model=PlanWorkspace)
def add_plan_chain(workspace_id: str, body: AddPlanChain, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.add_chain(workspace_id, body.chain, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/chain-updates", response_model=PlanWorkspace)
def update_plan_chain(workspace_id: str, body: UpdatePlanChain, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.update_chain(workspace_id, body.update, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/chain-links", response_model=PlanWorkspace)
def link_plan_chain_intent(workspace_id: str, body: LinkPlanChainIntent, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.link_chain_intent(workspace_id, body.link, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/decision", response_model=PlanWorkspace)
def submit_plan_decision(workspace_id: str, body: SubmitPlanDecision, container: Container = Depends(get_container)) -> PlanWorkspace:
    return container.plan.submit_decision(workspace_id, body.decision, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/status", response_model=PlanWorkspaceStatus)
def plan_workspace_status(workspace_id: str, body: PlanWorkspaceCommand, container: Container = Depends(get_container)) -> PlanWorkspaceStatus:
    return container.plan.status(workspace_id, lease_id=body.lease_id)


@router.post("/plan-workspaces/{workspace_id}/commit", response_model=PlanCommitResult)
def commit_plan_workspace(workspace_id: str, body: PlanWorkspaceCommand, container: Container = Depends(get_container)) -> PlanCommitResult:
    return container.plan.commit(workspace_id, lease_id=body.lease_id)
