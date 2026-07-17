from __future__ import annotations

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService
from axiom.modules.tasks.schemas import CreateTask
from axiom.modules.tasks.service import TaskService
from axiom.modules.vulnerabilities.external import MozzieAdapter
from axiom.modules.vulnerabilities.reporting import ExternalReportService
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


class FakeMozzieClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.payloads = []

    def resolve_project(self, backend_domain: str) -> str:
        assert backend_domain == "api.example.com"
        return "resolved-project"

    def report(self, payload):
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_external_report_attempt_is_persisted_and_retryable(tmp_path) -> None:
    engine, task = make_domain(tmp_path)
    vulnerability = VulnerabilityService(engine).report(task.id, sample_report())
    client = FakeMozzieClient([RuntimeError("temporary failure"), {"success": True, "id": 7}])
    service = ExternalReportService(engine, client=client)

    failed = service.report(task.id, vulnerability.id)
    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert failed.resolved_project == "resolved-project"
    assert "temporary failure" in failed.last_error

    succeeded = service.retry(failed.id)
    assert succeeded.status == "success"
    assert succeeded.attempt_count == 2
    assert succeeded.response["id"] == 7
    assert service.list(task.id, vulnerability.id)[0].id == failed.id
    assert client.payloads[0]["project"] == "resolved-project"
