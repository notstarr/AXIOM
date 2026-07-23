from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.modules.credentials.schemas import CreateCredential, Credential, UpdateCredential
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


@router.get("/projects/{project_id}/credentials", response_model=list[Credential])
def list_credentials(
    project_id: str,
    include_inactive: bool = False,
    service: CredentialService = Depends(get_service),
) -> list[Credential]:
    return service.list(project_id, active_only=not include_inactive)


@router.get("/projects/{project_id}/credentials/{credential_id}", response_model=Credential)
def get_project_credential(
    project_id: str,
    credential_id: str,
    service: CredentialService = Depends(get_service),
) -> Credential:
    try:
        credential = service.get(credential_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if credential.project_id != project_id:
        raise HTTPException(404, "Credential not found")
    return credential


@router.patch("/projects/{project_id}/credentials/{credential_id}", response_model=Credential)
def update_credential(
    project_id: str,
    credential_id: str,
    body: UpdateCredential,
    service: CredentialService = Depends(get_service),
) -> Credential:
    try:
        credential = service.get(credential_id)
        if credential.project_id != project_id:
            raise ValueError(f"Credential not found: {credential_id}")
        return service.update(credential_id, body)
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(404 if "not found:" in detail.lower() else 409, detail) from exc


@router.delete("/projects/{project_id}/credentials/{credential_id}", status_code=204)
def delete_credential(
    project_id: str,
    credential_id: str,
    service: CredentialService = Depends(get_service),
) -> None:
    try:
        credential = service.get(credential_id)
        if credential.project_id != project_id:
            raise ValueError(f"Credential not found: {credential_id}")
        service.delete(credential_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@internal_router.get("/{credential_id}", response_model=Credential)
def get_credential_secret(
    credential_id: str,
    service: CredentialService = Depends(get_service),
) -> Credential:
    try:
        return service.get(credential_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
