from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.settings.schemas import RuntimeSettings, UpdateRuntimeSettings


router = APIRouter(prefix="/settings/runtime", tags=["settings"])


@router.get("", response_model=RuntimeSettings)
def get_runtime_settings(
    container: Container = Depends(get_container),
) -> RuntimeSettings:
    return container.settings.get()


@router.put("", response_model=RuntimeSettings)
def update_runtime_settings(
    body: UpdateRuntimeSettings,
    container: Container = Depends(get_container),
) -> RuntimeSettings:
    return container.settings.update(body.config, actor=body.actor)
