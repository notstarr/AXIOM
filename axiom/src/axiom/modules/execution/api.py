from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.execution.schemas import (
    BootstrapCompleted,
    CompleteBootstrapCommand,
    CompleteExploreCommand,
    ExecutionInput,
    ExploreCompleted,
    LeaseCommand,
    ReleaseIntentCommand,
    ReleasedIntent,
    StartedIntent,
)


router = APIRouter(prefix="/dispatch", tags=["execution"])


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
