from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from axiom.api.dependencies import get_container
from axiom.composition import Container


router = APIRouter(prefix="/tasks", tags=["exports"])


@router.get("/{task_id}/export")
def export_task(
    task_id: str,
    format: str = Query(default="yaml"),
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    if format == "yaml":
        content = container.exports.overview_yaml(task_id)
    elif format == "json":
        content = container.exports.overview_json(task_id)
    elif format == "timeline":
        content = container.exports.timeline(task_id)
    else:
        from axiom.shared.errors import ConflictError

        raise ConflictError(f"Unsupported export format: {format}")
    return PlainTextResponse(content)


@router.get("/{task_id}/vulnerabilities-export")
def export_task_vulnerabilities(
    task_id: str,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    return PlainTextResponse(container.exports.vulnerabilities_markdown(task_id))


@router.get("/{task_id}/vulnerabilities/{vulnerability_id}/export")
def export_vulnerability(
    task_id: str,
    vulnerability_id: str,
    container: Container = Depends(get_container),
) -> PlainTextResponse:
    return PlainTextResponse(container.exports.vulnerability_markdown(task_id, vulnerability_id))
