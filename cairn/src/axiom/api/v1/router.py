from fastapi import APIRouter

from axiom.api.v1 import projects, tasks


router = APIRouter(prefix="/api/v1")
router.include_router(projects.router)
router.include_router(tasks.router)
