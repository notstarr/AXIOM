from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from axiom.modules.artifacts.service import ArtifactService
from axiom.modules.axiom_tools.service import AxiomToolService
from axiom.modules.archives.service import ArchiveService
from axiom.modules.audit.service import AuditService
from axiom.modules.context.service import ContextService
from axiom.modules.credentials.service import CredentialService
from axiom.modules.dashboard.service import DashboardService
from axiom.modules.dispatch.service import DispatchService
from axiom.modules.diagnostics.service import DiagnosticsService
from axiom.modules.execution.service import ExecutionService
from axiom.modules.exports.service import ExportService
from axiom.modules.graph.service import GraphService
from axiom.modules.projects.service import ProjectService
from axiom.modules.reason.service import ReasonService
from axiom.modules.mcp.service import MCPService
from axiom.modules.phase_capabilities.service import PhaseCapabilityService
from axiom.modules.external_reports.service import ExternalReportService
from axiom.modules.knowledge.service import KnowledgeService
from axiom.modules.skills.service import SkillService
from axiom.modules.guard.service import GuardService
from axiom.modules.goals.service import GoalService
from axiom.modules.settings.service import RuntimeSettingsService
from axiom.modules.tasks.service import TaskService
from axiom.modules.todos.service import TodoService
from axiom.modules.token_usage.service import TokenUsageService
from axiom.modules.interventions.service import InterventionService
from axiom.modules.vulnerabilities.service import VulnerabilityService
from axiom.modules.vulnerability_reviews.service import VulnerabilityReviewService
from axiom.modules.workers.service import WorkerService
from axiom.shared.storage import FileStore


@dataclass(frozen=True, slots=True)
class Container:
    engine: Engine
    projects: ProjectService
    tasks: TaskService
    graph: GraphService
    axiom_tools: AxiomToolService
    workers: WorkerService
    dispatch: DispatchService
    artifacts: ArtifactService
    archives: ArchiveService
    audit: AuditService
    store: FileStore
    execution: ExecutionService
    todos: TodoService
    reason: ReasonService
    context: ContextService
    settings: RuntimeSettingsService
    credentials: CredentialService
    vulnerabilities: VulnerabilityService
    vulnerability_reviews: VulnerabilityReviewService
    mcp: MCPService
    phase_capabilities: PhaseCapabilityService
    external_reports: ExternalReportService
    knowledge: KnowledgeService
    skills: SkillService
    guard: GuardService
    goals: GoalService
    exports: ExportService
    diagnostics: DiagnosticsService
    dashboard: DashboardService
    token_usage: TokenUsageService
    interventions: InterventionService

    @classmethod
    def build(cls, engine: Engine, *, data_dir: Path) -> "Container":
        store = FileStore(data_dir)
        vulnerability_reviews = VulnerabilityReviewService(engine, store)
        archives = ArchiveService(engine, store, reviews=vulnerability_reviews)
        context = ContextService(engine, store)
        reason = ReasonService(engine, store=store)
        skills = SkillService(data_dir, engine=engine)
        phase_capabilities = PhaseCapabilityService(engine, data_dir, skills=skills)
        axiom_tools = AxiomToolService(
            engine,
            data_dir,
            skills=skills,
            phase_capabilities=phase_capabilities,
        )
        axiom_tools.scan()
        settings = RuntimeSettingsService(engine)
        token_usage = TokenUsageService(engine)
        interventions = InterventionService(engine, store)
        mcp = MCPService(engine, phase_capabilities=phase_capabilities)
        return cls(
            engine=engine,
            projects=ProjectService(engine),
            tasks=TaskService(engine),
            graph=GraphService(engine, archives=archives),
            axiom_tools=axiom_tools,
            workers=WorkerService(engine),
            dispatch=DispatchService(engine, reviews=vulnerability_reviews),
            artifacts=ArtifactService(engine, store, archives=archives),
            archives=archives,
            audit=AuditService(engine),
            store=store,
            execution=ExecutionService(
                engine,
                store,
                context,
                reason,
                skills,
                axiom_tools,
                vulnerability_reviews,
            ),
            todos=TodoService(engine),
            reason=reason,
            context=context,
            settings=settings,
            credentials=CredentialService(engine),
            vulnerabilities=VulnerabilityService(
                engine,
                archives=archives,
                reviews=vulnerability_reviews,
            ),
            vulnerability_reviews=vulnerability_reviews,
            mcp=mcp,
            phase_capabilities=phase_capabilities,
            external_reports=ExternalReportService(engine, settings, store),
            knowledge=KnowledgeService(engine),
            skills=skills,
            guard=GuardService(engine, settings),
            goals=GoalService(engine),
            exports=ExportService(engine),
            diagnostics=DiagnosticsService(engine, token_usage=token_usage),
            dashboard=DashboardService(engine, token_usage=token_usage),
            token_usage=token_usage,
            interventions=interventions,
        )
