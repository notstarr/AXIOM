from __future__ import annotations

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService
from axiom.modules.vulnerabilities.external import MozzieAdapter
from axiom.modules.vulnerabilities.schemas import ReportVulnerability
from axiom.modules.vulnerabilities.service import VulnerabilityService


def make_domain(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    task = TaskService(engine).create(
        CreateTask(project_id=project.id, title="Task", origin="api.example.com", goal="finish")
    )
    return engine, task


def sample_report() -> ReportVulnerability:
    return ReportVulnerability(
        intent_id="i001",
        title="GET /api/users/{id} 越权访问",
        type="越权访问",
        severity="high",
        description="An authenticated user can read another user's profile.",
        prerequisites="A normal test account",
        impact="Cross-user private profile disclosure",
        reproduction_steps=["Login as test user", "Request another user's ID"],
        remediation="Enforce object-level authorization.",
        curl_command="curl -s 'https://api.example.com/api/users/2' -H 'Authorization: Bearer test-token'",
        evidence=["a001"],
        affected_target="api.example.com",
        status="待验证",
        source="bootstrap",
        reported_by="bootstrap-worker",
    )


def test_vulnerability_uses_generic_internal_model(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    service = VulnerabilityService(engine)
    vuln = service.report(task.id, sample_report())

    assert vuln.id == "v001"
    assert vuln.type == "越权访问"
    assert vuln.severity == "high"
    assert vuln.impact == "Cross-user private profile disclosure"
    assert service.list(task.id)[0].curl_command.startswith("curl ")


def test_mozzie_adapter_reuses_common_fields_and_adds_platform_project() -> None:
    report = sample_report()
    payload = MozzieAdapter().build_payload(report, resolved_project="demo-project")

    assert payload["vuln_name"] == report.title
    assert payload["vuln_type"] == "越权访问"
    assert payload["severity"] == "高危"
    assert payload["project"] == "demo-project"
    assert payload["curl_command"] == report.curl_command
    assert payload["description"] == {
        "danger": report.impact,
        "premise": report.prerequisites,
        "reproduce": "\n".join(report.reproduction_steps),
        "fix": report.remediation,
    }
