from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.phase_capabilities.schemas import (
    CapabilitySelection,
    PhaseCapabilitiesResponse,
    PhaseCapabilitiesUpdate,
)


router = APIRouter(prefix="/phase-capabilities", tags=["phase-capabilities"])


@router.get("", response_model=PhaseCapabilitiesResponse)
def get_phase_capabilities(
    container: Container = Depends(get_container),
) -> PhaseCapabilitiesResponse:
    return container.phase_capabilities.matrix()


@router.put("", response_model=PhaseCapabilitiesResponse)
def save_phase_capabilities(
    body: PhaseCapabilitiesUpdate,
    container: Container = Depends(get_container),
) -> PhaseCapabilitiesResponse:
    return container.phase_capabilities.save_matrix(body)


@router.put("/{kind}/{key}", response_model=PhaseCapabilitiesResponse)
def save_one_phase_capability(
    kind: str,
    key: str,
    body: CapabilitySelection,
    container: Container = Depends(get_container),
) -> PhaseCapabilitiesResponse:
    return container.phase_capabilities.save_capability(kind, key, body)
