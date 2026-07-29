"""Import every SQLAlchemy table exactly once for metadata discovery."""

from axiom.modules.artifacts.tables import artifacts
from axiom.modules.archives.tables import archive_batch_members, archive_batches
from axiom.modules.audit.tables import audit_events
from axiom.modules.context.tables import context_revisions
from axiom.modules.credentials.tables import credential_refreshes, credentials
from axiom.modules.graph.tables import facts, intent_sources, intents
from axiom.modules.knowledge.tables import project_knowledge
from axiom.modules.goals.tables import goal_evaluations
from axiom.modules.external_reports.tables import external_report_attempts
from axiom.modules.projects.tables import projects
from axiom.modules.reason.tables import reason_run_sources, reason_runs, reason_workspaces
from axiom.modules.mcp.tables import mcp_servers, mcp_tools
from axiom.modules.settings.tables import runtime_settings
from axiom.modules.tasks.tables import task_hints, tasks
from axiom.modules.todos.tables import todo_sources, todos
from axiom.modules.vulnerabilities.tables import vulnerability_artifacts, vulnerabilities
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
    artifacts,
    archive_batches,
    archive_batch_members,
    todos,
    todo_sources,
    reason_runs,
    reason_run_sources,
    reason_workspaces,
    context_revisions,
    runtime_settings,
    audit_events,
    credentials,
    credential_refreshes,
    vulnerabilities,
    vulnerability_artifacts,
    mcp_servers,
    mcp_tools,
    external_report_attempts,
    project_knowledge,
    goal_evaluations,
)
