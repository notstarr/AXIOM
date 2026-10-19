from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.webshell.schemas import (
    CreateWebShell,
    ExecCommand,
    UpdateWebShell,
    WebShellConnection,
    WebShellExecLog,
    WebShellExecResult,
    WebShellFileOp,
)

router = APIRouter(tags=["webshell"])


@router.get("/tasks/{task_id}/webshells", response_model=list[WebShellConnection])
def list_webshells(
    task_id: str,
    include_archived: bool = False,
    container: Container = Depends(get_container),
) -> list[WebShellConnection]:
    return container.webshells.list(task_id, include_archived=include_archived)


@router.post(
    "/tasks/{task_id}/webshells",
    response_model=WebShellConnection,
    status_code=status.HTTP_201_CREATED,
)
def create_webshell(
    task_id: str,
    body: CreateWebShell,
    actor: str = Query(default="human.frontend"),
    container: Container = Depends(get_container),
) -> WebShellConnection:
    return container.webshells.create(task_id, body, actor=actor)


@router.get("/tasks/{task_id}/webshells/{connection_id}", response_model=WebShellConnection)
def get_webshell(
    task_id: str,
    connection_id: str,
    container: Container = Depends(get_container),
) -> WebShellConnection:
    return container.webshells.get(task_id, connection_id)


@router.patch("/tasks/{task_id}/webshells/{connection_id}", response_model=WebShellConnection)
def update_webshell(
    task_id: str,
    connection_id: str,
    body: UpdateWebShell,
    container: Container = Depends(get_container),
) -> WebShellConnection:
    return container.webshells.update(task_id, connection_id, body)


@router.delete("/tasks/{task_id}/webshells/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_webshell(
    task_id: str,
    connection_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.webshells.archive(task_id, connection_id)


@router.post("/tasks/{task_id}/webshells/{connection_id}/test", response_model=WebShellExecResult)
def test_webshell(
    task_id: str,
    connection_id: str,
    actor: str = Query(default="human.frontend"),
    container: Container = Depends(get_container),
) -> WebShellExecResult:
    return container.webshells.test(task_id, connection_id, actor=actor)


@router.post("/tasks/{task_id}/webshells/{connection_id}/exec", response_model=WebShellExecResult)
def exec_webshell(
    task_id: str,
    connection_id: str,
    body: ExecCommand,
    actor: str = Query(default="human.frontend"),
    intent_id: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> WebShellExecResult:
    return container.webshells.exec(
        task_id,
        connection_id,
        body.command,
        actor=actor,
        intent_id=intent_id,
        timeout_seconds=body.timeout_seconds,
    )


@router.post("/tasks/{task_id}/webshells/{connection_id}/file", response_model=WebShellExecResult)
def webshell_file(
    task_id: str,
    connection_id: str,
    body: WebShellFileOp,
    actor: str = Query(default="human.frontend"),
    intent_id: str | None = Query(default=None),
    container: Container = Depends(get_container),
) -> WebShellExecResult:
    return container.webshells.file(task_id, connection_id, body, actor=actor, intent_id=intent_id)


@router.get("/tasks/{task_id}/webshells/{connection_id}/logs", response_model=list[WebShellExecLog])
def webshell_logs(
    task_id: str,
    connection_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    container: Container = Depends(get_container),
) -> list[WebShellExecLog]:
    return container.webshells.logs(task_id, connection_id, limit=limit)
