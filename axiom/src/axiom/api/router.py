from __future__ import annotations

from fastapi import APIRouter

from axiom.modules.artifacts.api import router as artifacts_router
from axiom.modules.axiom_tools.api import router as axiom_tools_router
from axiom.modules.archives.api import router as archives_router
from axiom.modules.audit.api import router as audit_router
from axiom.modules.auth.api import router as auth_router
from axiom.modules.context.api import router as context_router
from axiom.modules.credentials.api import router as credentials_router
from axiom.modules.dashboard.api import router as dashboard_router
from axiom.modules.dispatch.api import router as dispatch_router
from axiom.modules.diagnostics.api import router as diagnostics_router
from axiom.modules.execution.api import router as execution_router
from axiom.modules.exports.api import router as exports_router
from axiom.modules.knowledge.api import router as knowledge_router
from axiom.modules.skills.api import router as skills_router
from axiom.modules.guard.api import router as guard_router
from axiom.modules.goals.api import router as goals_router
from axiom.modules.graph.api import router as graph_router
from axiom.modules.projects.api import router as projects_router
from axiom.modules.mcp.api import router as mcp_router
from axiom.modules.phase_capabilities.api import router as phase_capabilities_router
from axiom.modules.plan.api import router as plan_router
from axiom.modules.reason.api import router as reason_router
from axiom.modules.settings.api import router as settings_router
from axiom.modules.storage_admin.api import router as storage_router
from axiom.modules.tasks.api import router as tasks_router
from axiom.modules.todos.api import router as todos_router
from axiom.modules.token_usage.api import router as token_usage_router
from axiom.modules.interventions.api import router as interventions_router
from axiom.modules.vulnerabilities.api import router as vulnerabilities_router
from axiom.modules.vulnerability_reviews.api import router as vulnerability_reviews_router
from axiom.modules.workers.api import phase_router as worker_phases_router
from axiom.modules.workers.api import router as workers_router
from axiom.modules.integrations.dingtalk_api import router as dingtalk_router
from axiom.modules.llm_gateway import llm_gateway_router


router = APIRouter(prefix="/api/v1")
router.include_router(projects_router)
router.include_router(dashboard_router)
router.include_router(reason_router)
router.include_router(plan_router)
router.include_router(tasks_router)
router.include_router(graph_router)
router.include_router(axiom_tools_router)
router.include_router(phase_capabilities_router)
router.include_router(worker_phases_router)
router.include_router(workers_router)
router.include_router(dispatch_router)
router.include_router(diagnostics_router)
router.include_router(artifacts_router)
router.include_router(archives_router)
router.include_router(audit_router)
router.include_router(context_router)
router.include_router(credentials_router)
router.include_router(execution_router)
router.include_router(exports_router)
router.include_router(todos_router)
router.include_router(settings_router)
router.include_router(storage_router)
router.include_router(vulnerabilities_router)
router.include_router(vulnerability_reviews_router)
router.include_router(mcp_router)
router.include_router(knowledge_router)
router.include_router(skills_router)
router.include_router(guard_router)
router.include_router(goals_router)
router.include_router(token_usage_router)
router.include_router(interventions_router)
router.include_router(auth_router)
router.include_router(dingtalk_router)
router.include_router(llm_gateway_router)
