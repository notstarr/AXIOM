from fastapi import APIRouter

from axiom.api.v1 import (
    artifacts,
    credentials,
    projects,
    reason_workspaces,
    tasks,
    todos,
    vulnerabilities,
)


router = APIRouter(prefix="/api/v1")
router.include_router(projects.router)
router.include_router(tasks.router)
router.include_router(todos.router)
router.include_router(reason_workspaces.router)
router.include_router(credentials.router)
router.include_router(credentials.internal_router)
router.include_router(artifacts.router)
router.include_router(vulnerabilities.router)
