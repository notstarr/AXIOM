from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.credentials.schemas import CredentialMaterial, WorkerCredentialSummary
from axiom.modules.execution.schemas import (
    ActivityCommand,
    BootstrapCompleted,
    CompleteBootstrapCommand,
    CompleteExploreCommand,
    CredentialAccessCommand,
    ExecutionInput,
    ExploreCompleted,
    ExploreVulnerabilityRecorded,
    LeaseCommand,
    ReportExploreVulnerabilityCommand,
    ReleaseIntentCommand,
    ReleasedIntent,
    StartedIntent,
)


router = APIRouter(prefix="/dispatch", tags=["execution"])


@router.post(
    "/tasks/{task_id}/credentials/list",
    response_model=list[WorkerCredentialSummary],
)
def list_worker_credentials(
    task_id: str,
    body: CredentialAccessCommand,
    container: Container = Depends(get_container),
) -> list[WorkerCredentialSummary]:
    return container.execution.credential_summaries(task_id=task_id, command=body)


@router.post(
    "/tasks/{task_id}/credentials/{credential_id}/get",
    response_model=CredentialMaterial,
)
def get_worker_credential(
    task_id: str,
    credential_id: str,
    body: CredentialAccessCommand,
    container: Container = Depends(get_container),
) -> CredentialMaterial:
    return container.execution.credential_material(
        task_id=task_id,
        credential_id=credential_id,
        command=body,
    )


@router.post("/tasks/{task_id}/intents/{intent_id}/start", response_model=StartedIntent)
def start_intent(
    task_id: str,
    intent_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> StartedIntent:
    return container.execution.start(task_id=task_id, intent_id=intent_id, command=body)


@router.post("/tasks/{task_id}/intents/{intent_id}/input", response_model=ExecutionInput)
def get_execution_input(
    task_id: str,
    intent_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> ExecutionInput:
    return container.execution.input(task_id=task_id, intent_id=intent_id, command=body)


@router.post("/tasks/{task_id}/intents/{intent_id}/heartbeat", status_code=204)
def heartbeat_intent(
    task_id: str,
    intent_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> None:
    container.execution.heartbeat(task_id=task_id, intent_id=intent_id, command=body)


@router.post("/tasks/{task_id}/intents/{intent_id}/activity", status_code=204)
def update_intent_activity(
    task_id: str,
    intent_id: str,
    body: ActivityCommand,
    container: Container = Depends(get_container),
) -> None:
    container.execution.activity(task_id=task_id, intent_id=intent_id, command=body)


@router.post("/tasks/{task_id}/intents/{intent_id}/conclude", status_code=204)
def begin_conclude(
    task_id: str,
    intent_id: str,
    body: LeaseCommand,
    container: Container = Depends(get_container),
) -> None:
    container.execution.begin_conclude(task_id=task_id, intent_id=intent_id, command=body)


@router.post(
    "/tasks/{task_id}/intents/{intent_id}/release",
    response_model=ReleasedIntent,
)
def release_intent(
    task_id: str,
    intent_id: str,
    body: ReleaseIntentCommand,
    container: Container = Depends(get_container),
) -> ReleasedIntent:
    return container.execution.release(task_id=task_id, intent_id=intent_id, command=body)


@router.post(
    "/tasks/{task_id}/intents/{intent_id}/bootstrap-complete",
    response_model=BootstrapCompleted,
)
def complete_bootstrap(
    task_id: str,
    intent_id: str,
    body: CompleteBootstrapCommand,
    container: Container = Depends(get_container),
) -> BootstrapCompleted:
    return container.execution.complete_bootstrap(
        task_id=task_id,
        intent_id=intent_id,
        command=body,
    )


@router.post(
    "/tasks/{task_id}/intents/{intent_id}/explore-complete",
    response_model=ExploreCompleted,
)
def complete_explore(
    task_id: str,
    intent_id: str,
    body: CompleteExploreCommand,
    container: Container = Depends(get_container),
) -> ExploreCompleted:
    return container.execution.complete_explore(
        task_id=task_id,
        intent_id=intent_id,
        command=body,
    )


@router.post(
    "/tasks/{task_id}/intents/{intent_id}/explore-vulnerabilities",
    response_model=ExploreVulnerabilityRecorded,
)
def report_explore_vulnerability(
    task_id: str,
    intent_id: str,
    body: ReportExploreVulnerabilityCommand,
    container: Container = Depends(get_container),
) -> ExploreVulnerabilityRecorded:
    return container.execution.report_explore_vulnerability(
        task_id=task_id,
        intent_id=intent_id,
        command=body,
    )
