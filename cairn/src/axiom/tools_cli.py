from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click
import requests


TODO_CATEGORIES = (
    "recon",
    "authentication",
    "authorization",
    "injection",
    "business_logic",
    "file",
    "network",
    "configuration",
    "client_side",
    "other",
)


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
        raise click.BadParameter(
            "source must use TYPE:ID, for example origin:origin, fact:f001, or hint:h001"
        )
    return {"type": source_type.strip(), "id": source_id.strip()}


@click.group()
def main() -> None:
    """Structured AXIOM tools for Bootstrap, Reason, and Explore workers."""


@main.group()
def todo() -> None:
    """Read formal Todos or edit the current Reason draft."""


@todo.command("list")
def todo_list() -> None:
    _emit(_request("GET", f"/api/v1/tasks/{_task_id()}/todos"))


@todo.command("add")
@click.option("--ref", required=True)
@click.option("--title", required=True)
@click.option("--category", type=click.Choice(TODO_CATEGORIES), required=True)
@click.option("--theme", default="")
@click.option("--priority", type=click.IntRange(0, 100), default=50)
@click.option(
    "--source",
    "sources",
    multiple=True,
    required=True,
    callback=lambda _c, _p, values: [_source(v) for v in values],
    help="Evidence reference TYPE:ID (origin:origin, fact:f001, hint:h001, knowledge:k001, artifact:a001, vulnerability:v001).",
)
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


@reason.command("submit")
@click.option(
    "--file",
    "plan_file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    required=True,
    help="JSON file containing todo_adds, todo_updates, and intent_proposals.",
)
def reason_submit(plan_file: str) -> None:
    try:
        with open(plan_file, "r", encoding="utf-8") as handle:
            body = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"invalid Reason plan file: {exc}") from exc
    if not isinstance(body, dict):
        raise click.ClickException("Reason plan file must contain a JSON object")
    _emit(
        _request(
            "POST",
            f"/api/v1/reason-workspaces/{_workspace_id()}/submit",
            body,
        )
    )


@main.group()
def task() -> None:
    """Task-level Reason decisions."""


@task.command("complete")
@click.option("--description", required=True)
def task_complete(description: str) -> None:
    _emit(
        _request(
            "POST",
            f"/api/v1/internal/tasks/{_task_id()}/complete",
            {"description": description},
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
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default="",
    help="Write the private credential JSON here; stdout contains metadata only.",
)
def credential_get(credential_id: str, output_path: str) -> None:
    """Fetch credential metadata without leaking secret values to the transcript.

    Explore workers can use ``--output`` and read the resulting 0600 file from
    inside a command. Returning the secret on stdout would make Pi persist it
    in its JSONL session and can also tempt a model to copy it into a later
    command, so the safe default is always redacted metadata.
    """

    payload = _request("GET", f"/api/v1/internal/credentials/{credential_id}")
    if not isinstance(payload, dict):
        raise click.ClickException("credential response must be a JSON object")

    output = output_path.strip()
    if output:
        path = Path(output).expanduser()
        run_dir = os.getenv("AXIOM_RUN_DIR", "").strip()
        if run_dir and not path.resolve().is_relative_to(Path(run_dir).expanduser().resolve()):
            raise click.ClickException("credential output must stay inside AXIOM_RUN_DIR")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            raise click.ClickException(f"unable to write private credential file: {exc}") from exc

    safe: dict[str, Any] = {
        key: value
        for key, value in payload.items()
        if key not in {"data", "credentials", "credentials_json", "secret", "token"}
    }
    raw_data = payload.get("data")
    if isinstance(raw_data, dict):
        safe["data"] = {
            "redacted": True,
            "fields": sorted(str(key) for key in raw_data),
        }
    elif raw_data is not None:
        safe["data"] = {"redacted": True}
    if output:
        safe["written_to"] = str(path)
    else:
        safe["hint"] = "Use --output <private-file> when the worker needs the credential for a request."
    _emit(safe)


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


@main.group("context")
def context_group() -> None:
    """Expand compact Context Manifest indexes on demand."""


@context_group.command("get")
@click.option("--fact", "facts", multiple=True)
@click.option("--intent", "intents", multiple=True)
@click.option("--todo", "todos", multiple=True)
@click.option("--knowledge", "knowledge", multiple=True)
@click.option("--artifact", "artifacts", multiple=True)
@click.option("--vulnerability", "vulnerabilities", multiple=True)
def context_get(**values) -> None:
    body = {
        key: list(dict.fromkeys(item.strip() for item in items if item.strip()))
        for key, items in values.items()
    }
    if not any(body.values()):
        raise click.ClickException("select at least one Fact, Intent, Todo, Knowledge, Artifact, or Vulnerability ID")
    _emit(
        _request(
            "POST",
            f"/api/v1/internal/tasks/{_task_id()}/context/expand",
            body,
        )
    )


@context_group.command("related")
@click.option("--intent", "intent_id", required=True)
def context_related(intent_id: str) -> None:
    _emit(
        _request(
            "GET",
            f"/api/v1/internal/tasks/{_task_id()}/context/intents/{intent_id.strip()}/related",
        )
    )


@main.group()
def vulnerability() -> None:
    """Report confirmed vulnerabilities."""


@vulnerability.command("list")
def vulnerability_list() -> None:
    _emit(_request("GET", f"/api/v1/tasks/{_task_id()}/vulnerabilities"))


@vulnerability.command("report")
@click.option("--file", "json_file", type=click.Path(exists=True, dir_okay=False), required=True)
def vulnerability_report(json_file: str) -> None:
    try:
        with open(json_file, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"invalid vulnerability report file: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException("vulnerability report file must contain a JSON object")

    # Worker reports should always be attributable to the currently executing
    # Intent.  Models occasionally omit these bookkeeping fields even though
    # the runner already exposes authoritative values in the environment.
    # Fill only missing values so explicit human/test payloads remain usable.
    payload = dict(payload)
    intent_id = str(payload.get("intent_id") or os.getenv("AXIOM_INTENT_ID", "")).strip()
    worker = str(payload.get("reported_by") or os.getenv("AXIOM_WORKER", "")).strip()
    phase = os.getenv("AXIOM_PHASE", "").strip().lower()
    source = str(payload.get("source") or phase or "explore").strip()
    if not intent_id:
        raise click.ClickException("vulnerability report requires AXIOM_INTENT_ID or intent_id")
    if not worker:
        raise click.ClickException("vulnerability report requires AXIOM_WORKER or reported_by")
    payload["intent_id"] = intent_id
    payload["reported_by"] = worker
    payload["source"] = source

    required_text = ("affected_target", "description", "impact")
    missing_text = [
        key for key in required_text if not str(payload.get(key) or "").strip()
    ]
    required_lists = ("evidence", "reproduction_steps")
    missing_lists = [
        key
        for key in required_lists
        if not isinstance(payload.get(key), list)
        or not any(str(item).strip() for item in payload[key])
    ]
    missing = [*missing_text, *missing_lists]
    if missing:
        raise click.ClickException(
            "vulnerability report is incomplete; missing non-empty fields: "
            + ", ".join(missing)
        )
    _emit(
        _request(
            "POST",
            f"/api/v1/internal/tasks/{_task_id()}/vulnerabilities",
            payload,
        )
    )
