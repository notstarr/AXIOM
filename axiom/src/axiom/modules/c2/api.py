from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.c2.schemas import (
    C2ExecCommand,
    C2Listener,
    C2Oneliner,
    C2Session,
    C2Task,
    CreateListener,
    SetSessionSleep,
)

router = APIRouter(tags=["c2"])


@router.get("/tasks/{task_id}/c2/listeners", response_model=list[C2Listener])
def list_listeners(
    task_id: str,
    include_archived: bool = False,
    container: Container = Depends(get_container),
) -> list[C2Listener]:
    return container.c2.list_listeners(task_id, include_archived=include_archived)


@router.post(
    "/tasks/{task_id}/c2/listeners",
    response_model=C2Listener,
    status_code=status.HTTP_201_CREATED,
)
def create_listener(
    task_id: str,
    body: CreateListener,
    actor: str = Query(default="human.frontend"),
    container: Container = Depends(get_container),
) -> C2Listener:
    return container.c2.create_listener(task_id, body, actor=actor)


@router.get("/tasks/{task_id}/c2/listeners/{listener_id}", response_model=C2Listener)
def get_listener(
    task_id: str,
    listener_id: str,
    container: Container = Depends(get_container),
) -> C2Listener:
    return container.c2.get_listener(task_id, listener_id)


@router.post("/tasks/{task_id}/c2/listeners/{listener_id}/start", response_model=C2Listener)
def start_listener(
    task_id: str,
    listener_id: str,
    container: Container = Depends(get_container),
) -> C2Listener:
    return container.c2.start_listener(task_id, listener_id)


@router.post("/tasks/{task_id}/c2/listeners/{listener_id}/stop", response_model=C2Listener)
def stop_listener(
    task_id: str,
    listener_id: str,
    container: Container = Depends(get_container),
) -> C2Listener:
    return container.c2.stop_listener(task_id, listener_id)


@router.delete("/tasks/{task_id}/c2/listeners/{listener_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_listener(
    task_id: str,
    listener_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.c2.archive_listener(task_id, listener_id)


@router.get("/tasks/{task_id}/c2/listeners/{listener_id}/oneliner", response_model=C2Oneliner)
def listener_oneliner(
    task_id: str,
    listener_id: str,
    container: Container = Depends(get_container),
) -> C2Oneliner:
    return container.c2.oneliner(task_id, listener_id)


@router.get("/tasks/{task_id}/c2/sessions", response_model=list[C2Session])
def list_sessions(
    task_id: str,
    listener_id: str | None = None,
    container: Container = Depends(get_container),
) -> list[C2Session]:
    return container.c2.list_sessions(task_id, listener_id=listener_id)


@router.put("/tasks/{task_id}/c2/sessions/{session_id}/sleep", response_model=C2Session)
def set_session_sleep(
    task_id: str,
    session_id: str,
    body: SetSessionSleep,
    container: Container = Depends(get_container),
) -> C2Session:
    return container.c2.set_sleep(task_id, session_id, body)


@router.post("/tasks/{task_id}/c2/sessions/{session_id}/kill", response_model=C2Session)
def kill_session(
    task_id: str,
    session_id: str,
    container: Container = Depends(get_container),
) -> C2Session:
    return container.c2.kill_session(task_id, session_id)


@router.post("/tasks/{task_id}/c2/sessions/{session_id}/exec", response_model=C2Task)
def exec_session(
    task_id: str,
    session_id: str,
    body: C2ExecCommand,
    actor: str = Query(default="human.frontend"),
    intent_id: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> C2Task:
    return container.c2.exec(task_id, session_id, body, actor=actor, intent_id=intent_id)


@router.get("/tasks/{task_id}/c2/tasks", response_model=list[C2Task])
def list_tasks(
    task_id: str,
    session_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> list[C2Task]:
    return container.c2.list_tasks(task_id, session_id=session_id, limit=limit)


@router.get("/tasks/{task_id}/c2/tasks/{c2_task_id}", response_model=C2Task)
def get_task(
    task_id: str,
    c2_task_id: str,
    container: Container = Depends(get_container),
) -> C2Task:
    return container.c2.get_task(task_id, c2_task_id)
