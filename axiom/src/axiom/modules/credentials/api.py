from __future__ import annotations

from fastapi import APIRouter, Depends, status

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.credentials.schemas import Credential, CredentialInput


router = APIRouter(prefix="/projects/{project_id}/credentials", tags=["credentials"])


@router.post("", response_model=Credential, status_code=status.HTTP_201_CREATED)
def create_credential(
    project_id: str,
    body: CredentialInput,
    container: Container = Depends(get_container),
) -> Credential:
    return container.credentials.create(project_id, body)


@router.get("", response_model=list[Credential])
def list_credentials(
    project_id: str,
    container: Container = Depends(get_container),
) -> list[Credential]:
    return container.credentials.list(project_id)


@router.get("/{credential_id}", response_model=Credential)
def get_credential(
    project_id: str,
    credential_id: str,
    container: Container = Depends(get_container),
) -> Credential:
    return container.credentials.get(project_id, credential_id)


@router.put("/{credential_id}", response_model=Credential)
def update_credential(
    project_id: str,
    credential_id: str,
    body: CredentialInput,
    container: Container = Depends(get_container),
) -> Credential:
    return container.credentials.update(project_id, credential_id, body)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    project_id: str,
    credential_id: str,
    container: Container = Depends(get_container),
) -> None:
    container.credentials.delete(project_id, credential_id)
