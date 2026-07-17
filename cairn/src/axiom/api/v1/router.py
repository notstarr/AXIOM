from fastapi import APIRouter

from axiom.api.v1 import (
    artifacts,
    audit,
    credentials,
    dispatcher,
    lifecycle,
    knowledge,
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
router.include_router(audit.router)
router.include_router(audit.internal)
router.include_router(vulnerabilities.router)
router.include_router(lifecycle.router)
router.include_router(knowledge.router)
router.include_router(lifecycle.internal)
router.include_router(dispatcher.router)
router.include_router(dispatcher.internal)
