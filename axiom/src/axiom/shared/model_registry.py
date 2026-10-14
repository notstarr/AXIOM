"""Import every SQLAlchemy table exactly once for metadata discovery."""

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.axiom_tools.tables import axiom_tools, worker_axiom_tool_bindings
from axiom.modules.archives.tables import archive_batch_members, archive_batches
from axiom.modules.audit.tables import audit_events
from axiom.modules.context.tables import context_revisions
from axiom.modules.credentials.tables import credential_refreshes, credentials
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.goals.tables import goal_evaluations, task_goal_cycles
from axiom.modules.plan.tables import plan_run_sources, plan_runs, plan_workspaces
from axiom.modules.projects.tables import projects
from axiom.modules.reason.tables import reason_run_sources, reason_runs, reason_workspaces
from axiom.modules.redteam.tables import red_team_chains, red_team_results
from axiom.modules.sandboxes.tables import task_sandboxes
from axiom.modules.mcp.tables import mcp_servers, mcp_tools
from axiom.modules.phase_capabilities.tables import phase_capability_bindings
from axiom.modules.settings.tables import runtime_settings
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.token_usage.tables import llm_token_usage, llm_token_usage_sources
from axiom.modules.interventions.tables import intent_interventions
from axiom.modules.todos.tables import todo_sources, todos
from axiom.modules.vulnerabilities.tables import vulnerability_artifacts, vulnerabilities
from axiom.modules.vulnerability_reviews.tables import vulnerability_review_runs
from axiom.modules.workers.tables import workers
from axiom.shared.ids import id_counters


ALL_TABLES = (
    id_counters,
    projects,
    tasks,
    task_hints,
    facts,
    intents,
    intent_sources,
    workers,
    axiom_tools,
    worker_axiom_tool_bindings,
    phase_capability_bindings,
    artifacts,
    archive_batches,
    archive_batch_members,
    todos,
    todo_sources,
    reason_runs,
    reason_run_sources,
    reason_workspaces,
    task_sandboxes,
    plan_runs,
    plan_run_sources,
    plan_workspaces,
    context_revisions,
    runtime_settings,
    audit_events,
    credentials,
    credential_refreshes,
    vulnerabilities,
    vulnerability_review_runs,
    vulnerability_artifacts,
    mcp_servers,
    mcp_tools,
    project_knowledge,
    task_goal_cycles,
    goal_evaluations,
    llm_token_usage,
    llm_token_usage_sources,
    intent_interventions,
    red_team_chains,
    red_team_results,
)
