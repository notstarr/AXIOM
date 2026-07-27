from __future__ import annotations

from fastapi import APIRouter, Depends, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.credentials.schemas import (
    Credential,
    CredentialInput,
    CredentialRefresh,
    CredentialSummary,
    SetCredentialEnabled,
)


router = APIRouter(prefix="/projects/{project_id}", tags=["credentials"])


@router.post(
    "/credentials",
    response_model=Credential,
    status_code=status.HTTP_201_CREATED,
)
def create_credential(
    project_id: str,
    body: CredentialInput,
    container: Container = Depends(get_container),
) -> Credential:
    return container.credentials.create(project_id, body)


@router.get("/credentials", response_model=list[CredentialSummary])
def list_credentials(
    project_id: str,
    container: Container = Depends(get_container),
) -> list[CredentialSummary]:
    return container.credentials.list(project_id)


@router.get("/credentials/{credential_id}", response_model=Credential)
def get_credential(
    project_id: str,
    credential_id: str,
    container: Container = Depends(get_container),
) -> Credential:
    return container.credentials.get(project_id, credential_id)


@router.get(
    "/credential-refreshes/{refresh_id}",
    response_model=CredentialRefresh,
)
def get_credential_refresh(
    project_id: str,
    refresh_id: str,
    container: Container = Depends(get_container),
) -> CredentialRefresh:
    return container.credentials.get_refresh(project_id, refresh_id)


@router.put("/credentials/{credential_id}", response_model=Credential)
def update_credential(
    project_id: str,
    credential_id: str,
    body: CredentialInput,
    container: Container = Depends(get_container),
) -> Credential:
    return container.credentials.update(project_id, credential_id, body)


@router.patch(
    "/credentials/{credential_id}/enabled",
    response_model=CredentialSummary,
)
def set_credential_enabled(
    project_id: str,
    credential_id: str,
    body: SetCredentialEnabled,
    container: Container = Depends(get_container),
) -> CredentialSummary:
    return container.credentials.set_enabled(project_id, credential_id, body)


@router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_credential(
    project_id: str,
    credential_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.credentials.delete(project_id, credential_id)
