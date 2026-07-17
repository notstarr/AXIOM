from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Request
from sqlalchemy import Engine

from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.service import TaskService


def get_v2_engine(request: Request) -> Engine:
    engine = getattr(request.app.state, "v2_engine", None)
    if engine is None:
        raise RuntimeError("V2 database engine is not configured")
    return engine


def get_project_service(request: Request) -> ProjectService:
    return ProjectService(get_v2_engine(request))


def get_task_service(request: Request) -> TaskService:
    return TaskService(get_v2_engine(request))


def require_service_token(
    request: Request,
    authorization: str = Header(default=""),
) -> None:
    expected = str(
        getattr(request.app.state, "service_token", "")
        or os.getenv("AXIOM_SERVICE_TOKEN", "")
    ).strip()
    if not expected:
        raise HTTPException(503, "AXIOM Service Token is not configured")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
        raise HTTPException(401, "Invalid AXIOM Service Token")
