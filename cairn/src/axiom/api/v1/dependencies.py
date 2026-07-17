from __future__ import annotations

from fastapi import Request
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
