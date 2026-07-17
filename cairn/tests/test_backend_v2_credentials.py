from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.api.v1.router import router
from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.modules.credentials.schemas import CreateCredential
from axiom.modules.credentials.service import CredentialService
from axiom.modules.projects.schemas import CreateProject
from axiom.modules.projects.service import ProjectService


def make_domain(tmp_path):
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    project = ProjectService(engine).create(CreateProject(title="Demo"))
    return engine, project


def test_project_credentials_are_plaintext_and_project_shared(tmp_path) -> None:
    engine, project = make_domain(tmp_path)
    service = CredentialService(engine)
    credential = service.create(
        project.id,
        CreateCredential(
            label="admin-session",
            auth_type="cookie",
            data={"Cookie": "sid=plain-test-value"},
            domains=["api.example.com"],
            notes="test account",
        ),
    )

    assert credential.id == "cred_000001"
    assert credential.data["Cookie"] == "sid=plain-test-value"
    assert service.list(project.id)[0].data == credential.data
    assert service.get(credential.id).project_id == project.id


def test_v1_credential_secret_endpoint_requires_service_token(tmp_path) -> None:
    engine, project = make_domain(tmp_path)
    app = FastAPI()
    app.state.v2_engine = engine
    app.state.service_token = "service-token"
    app.include_router(router)

    with TestClient(app) as client:
        created = client.post(
            f"/api/v1/projects/{project.id}/credentials",
            json={
                "label": "jwt-user",
                "auth_type": "jwt",
                "data": {"Authorization": "Bearer plaintext"},
                "domains": ["api.example.com"],
            },
        )
        assert created.status_code == 201
        credential_id = created.json()["id"]

        listed = client.get(f"/api/v1/projects/{project.id}/credentials")
        assert listed.status_code == 200
        assert "data" not in listed.json()[0]

        assert client.get(f"/api/v1/internal/credentials/{credential_id}").status_code == 401
        fetched = client.get(
            f"/api/v1/internal/credentials/{credential_id}",
            headers={"Authorization": "Bearer service-token"},
        )
        assert fetched.status_code == 200
        assert fetched.json()["data"]["Authorization"] == "Bearer plaintext"
