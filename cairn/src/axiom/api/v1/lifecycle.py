from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.graph.schemas import ConcludeResult, Intent
from axiom.modules.graph.service import IntentService
from axiom.modules.tasks.lifecycle import TaskLifecycleService
from axiom.modules.tasks.schemas import Task


router = APIRouter(tags=["v1-lifecycle"])
internal = APIRouter(
    prefix="/internal",
    tags=["v1-internal-lifecycle"],
    dependencies=[Depends(require_service_token)],
)


class WorkerCommand(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def require_worker(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("worker must not be empty")
        return text


class ReleaseIntentCommand(WorkerCommand):
    failure_kind: str = ""
    next_retry_at: str | None = None


class ConcludeIntentCommand(WorkerCommand):
    description: str


class CompleteTaskCommand(BaseModel):
    description: str


class NeedsReplanCommand(BaseModel):
    reason: str = "retry budget exhausted"


def intent_service(engine=Depends(get_v2_engine)) -> IntentService:
    return IntentService(engine)


def task_lifecycle(engine=Depends(get_v2_engine)) -> TaskLifecycleService:
    return TaskLifecycleService(engine)


def _raise(exc: ValueError) -> None:
    detail = str(exc)
    lowered = detail.lower()
    if "not found:" in lowered:
        raise HTTPException(404, detail) from exc
    if "claimed" in lowered or "not running" in lowered or "cannot" in lowered:
        raise HTTPException(409, detail) from exc
    raise HTTPException(422, detail) from exc


@internal.post("/tasks/{task_id}/intents/{intent_id}/claim", response_model=Intent)
def claim_intent(task_id: str, intent_id: str, body: WorkerCommand, service=Depends(intent_service)):
    try:
        return service.claim(task_id, intent_id, worker=body.worker)
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/intents/{intent_id}/heartbeat", response_model=Intent)
def heartbeat_intent(task_id: str, intent_id: str, body: WorkerCommand, service=Depends(intent_service)):
    try:
        return service.heartbeat(task_id, intent_id, worker=body.worker)
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/intents/{intent_id}/release", response_model=Intent)
def release_intent(task_id: str, intent_id: str, body: ReleaseIntentCommand, service=Depends(intent_service)):
    try:
        return service.release(
            task_id,
            intent_id,
            worker=body.worker,
            failure_kind=body.failure_kind,
            next_retry_at=body.next_retry_at,
        )
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/intents/{intent_id}/conclude", response_model=ConcludeResult)
def conclude_intent(task_id: str, intent_id: str, body: ConcludeIntentCommand, service=Depends(intent_service)):
    try:
        return service.conclude(
            task_id,
            intent_id,
            worker=body.worker,
            description=body.description,
        )
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/intents/{intent_id}/needs-replan", response_model=Intent)
def needs_replan(
    task_id: str,
    intent_id: str,
    body: NeedsReplanCommand,
    service=Depends(intent_service),
):
    try:
        return service.needs_replan(task_id, intent_id, reason=body.reason)
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/reason/claim", response_model=Task)
def claim_reason(task_id: str, body: WorkerCommand, service=Depends(task_lifecycle)):
    try:
        return service.claim_reason(task_id, worker=body.worker)
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/reason/heartbeat", response_model=Task)
def heartbeat_reason(task_id: str, body: WorkerCommand, service=Depends(task_lifecycle)):
    try:
        return service.heartbeat_reason(task_id, worker=body.worker)
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/reason/release", response_model=Task)
def release_reason(task_id: str, body: WorkerCommand, service=Depends(task_lifecycle)):
    try:
        return service.release_reason(task_id, worker=body.worker)
    except ValueError as exc:
        _raise(exc)


@router.post("/tasks/{task_id}/stop", response_model=Task)
def stop_task(task_id: str, service=Depends(task_lifecycle)):
    try:
        return service.stop(task_id)
    except ValueError as exc:
        _raise(exc)


@router.post("/tasks/{task_id}/resume", response_model=Task)
def resume_task(task_id: str, service=Depends(task_lifecycle)):
    try:
        return service.resume(task_id)
    except ValueError as exc:
        _raise(exc)


@internal.post("/tasks/{task_id}/complete", response_model=Task)
def complete_task(task_id: str, body: CompleteTaskCommand, service=Depends(task_lifecycle)):
    try:
        return service.complete(task_id, description=body.description)
    except ValueError as exc:
        _raise(exc)
