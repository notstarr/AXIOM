from fastapi import APIRouter

from axiom.api.v1 import projects, reason_workspaces, tasks, todos


router = APIRouter(prefix="/api/v1")
router.include_router(projects.router)
router.include_router(tasks.router)
router.include_router(todos.router)
router.include_router(reason_workspaces.router)
