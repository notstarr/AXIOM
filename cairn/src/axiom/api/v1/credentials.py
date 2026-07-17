from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.credentials.schemas import CreateCredential, Credential, CredentialSummary
from axiom.modules.credentials.service import CredentialService


router = APIRouter(tags=["v1-credentials"])
internal_router = APIRouter(
    prefix="/internal/credentials",
    tags=["v1-internal-credentials"],
    dependencies=[Depends(require_service_token)],
)


def get_service(engine=Depends(get_v2_engine)) -> CredentialService:
    return CredentialService(engine)


@router.post("/projects/{project_id}/credentials", response_model=Credential, status_code=201)
def create_credential(
    project_id: str,
    body: CreateCredential,
    service: CredentialService = Depends(get_service),
) -> Credential:
    try:
        return service.create(project_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if detail.startswith("Project not found:") else 409, detail) from exc


@router.get("/projects/{project_id}/credentials", response_model=list[CredentialSummary])
def list_credentials(
    project_id: str,
    service: CredentialService = Depends(get_service),
) -> list[CredentialSummary]:
    return [CredentialSummary.model_validate(item.model_dump()) for item in service.list(project_id)]


@internal_router.get("/{credential_id}", response_model=Credential)
def get_credential_secret(
    credential_id: str,
    service: CredentialService = Depends(get_service),
) -> Credential:
    try:
        return service.get(credential_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
