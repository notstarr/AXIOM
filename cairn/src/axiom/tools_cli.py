from __future__ import annotations

import json
import os
from typing import Any

import click
import requests


def _server() -> str:
    return os.getenv("AXIOM_SERVER", "http://127.0.0.1:8888").rstrip("/")


def _workspace_id() -> str:
    value = os.getenv("AXIOM_REASON_WORKSPACE", "").strip()
    if not value:
        raise click.ClickException("AXIOM_REASON_WORKSPACE is not configured")
    return value


def _task_id() -> str:
    value = os.getenv("AXIOM_TASK_ID", "").strip()
    if not value:
        raise click.ClickException("AXIOM_TASK_ID is not configured")
    return value


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = os.getenv("AXIOM_SERVICE_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    try:
        response = requests.request(
            method,
            f"{_server()}{path}",
            headers=_headers(),
            json=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise click.ClickException(str(exc)) from exc
    if not response.ok:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise click.ClickException(f"HTTP {response.status_code}: {detail}")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _emit(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _source(value: str) -> dict[str, str]:
    source_type, separator, source_id = value.partition(":")
    if not separator or not source_type.strip() or not source_id.strip():
        raise click.BadParameter("source must use TYPE:ID")
    return {"type": source_type.strip(), "id": source_id.strip()}


@click.group()
def main() -> None:
    """Structured AXIOM tools for Reason and Explore workers."""


@main.group()
def todo() -> None:
    """Read formal Todos or edit the current Reason draft."""


@todo.command("list")
def todo_list() -> None:
    _emit(_request("GET", f"/api/v1/tasks/{_task_id()}/todos"))


@todo.command("add")
@click.option("--ref", required=True)
@click.option("--title", required=True)
@click.option("--category", required=True)
@click.option("--theme", default="")
@click.option("--priority", type=click.IntRange(0, 100), default=50)
@click.option("--source", "sources", multiple=True, required=True, callback=lambda _c, _p, values: [_source(v) for v in values])
def todo_add(ref: str, title: str, category: str, theme: str, priority: int, sources) -> None:
    _emit(
        _request(
            "POST",
            f"/api/v1/reason-workspaces/{_workspace_id()}/todos",
            {
                "ref": ref,
                "title": title,
                "category": category,
                "theme": theme,
                "priority": priority,
                "sources": list(sources),
            },
        )
    )


@todo.command("update")
@click.option("--id", "todo_id", required=True)
@click.option("--title")
@click.option("--category")
@click.option("--theme")
@click.option("--priority", type=click.IntRange(0, 100))
@click.option("--status", type=click.Choice(["pending", "active", "completed", "rejected", "obsolete"]))
@click.option("--reason", "status_reason")
def todo_update(todo_id: str, **values) -> None:
    body = {"todo_id": todo_id, **{key: value for key, value in values.items() if value is not None}}
    _emit(
        _request(
            "POST",
            f"/api/v1/reason-workspaces/{_workspace_id()}/todo-updates",
            body,
        )
    )


@main.group()
def intent() -> None:
    """Propose an Intent in the current Reason draft."""


@intent.command("propose")
@click.option("--todo-ref")
@click.option("--todo-id")
@click.option("--from-fact", "from_facts", multiple=True, required=True)
@click.option("--description", required=True)
@click.option("--target", "targets", multiple=True, required=True)
@click.option("--execute-seconds", type=click.IntRange(min=1), required=True)
@click.option("--conclude-seconds", type=click.IntRange(min=1), required=True)
def intent_propose(
    todo_ref: str | None,
    todo_id: str | None,
    from_facts,
    description: str,
    targets,
    execute_seconds: int,
    conclude_seconds: int,
) -> None:
    _emit(
        _request(
            "POST",
            f"/api/v1/reason-workspaces/{_workspace_id()}/intents",
            {
                "todo_ref": todo_ref,
                "todo_id": todo_id,
                "from_facts": list(from_facts),
                "description": description,
                "targets": list(targets),
                "execute_seconds": execute_seconds,
                "conclude_seconds": conclude_seconds,
            },
        )
    )


@main.group()
def reason() -> None:
    """Commit the current Reason draft."""


@reason.command("commit")
def reason_commit() -> None:
    _emit(
        _request(
            "POST",
            f"/api/v1/reason-workspaces/{_workspace_id()}/commit",
        )
    )


@main.group()
def credential() -> None:
    """Read Project credentials for Explore."""


@credential.command("list")
@click.option("--project-id", default=lambda: os.getenv("AXIOM_PROJECT_ID", ""))
def credential_list(project_id: str) -> None:
    project_id = project_id.strip()
    if not project_id:
        raise click.ClickException("AXIOM_PROJECT_ID is not configured")
    _emit(_request("GET", f"/api/v1/projects/{project_id}/credentials"))


@credential.command("get")
@click.argument("credential_id")
def credential_get(credential_id: str) -> None:
    _emit(_request("GET", f"/api/v1/internal/credentials/{credential_id}"))


@main.group()
def artifact() -> None:
    """Register and list Task artifacts."""


@artifact.command("list")
def artifact_list() -> None:
    _emit(_request("GET", f"/api/v1/tasks/{_task_id()}/artifacts"))


@artifact.command("register")
@click.option("--type", "artifact_type", required=True)
@click.option("--path", required=True)
@click.option("--summary", default="")
@click.option("--intent-id", default=lambda: os.getenv("AXIOM_INTENT_ID", ""))
@click.option("--actor", default=lambda: os.getenv("AXIOM_WORKER", "worker"))
def artifact_register(
    artifact_type: str,
    path: str,
    summary: str,
    intent_id: str,
    actor: str,
) -> None:
    _emit(
        _request(
            "POST",
            f"/api/v1/internal/tasks/{_task_id()}/artifacts",
            {
                "intent_id": intent_id.strip() or None,
                "artifact_type": artifact_type,
                "path": path,
                "summary": summary,
                "actor": actor,
            },
        )
    )


@main.group()
def vulnerability() -> None:
    """Report confirmed vulnerabilities."""


@vulnerability.command("report")
@click.option("--file", "json_file", type=click.Path(exists=True, dir_okay=False), required=True)
def vulnerability_report(json_file: str) -> None:
    with open(json_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    _emit(
        _request(
            "POST",
            f"/api/v1/internal/tasks/{_task_id()}/vulnerabilities",
            payload,
        )
    )
