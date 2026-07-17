from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.dispatcher.context.builder import ContextBuilder, ContextSnapshot


router = APIRouter(
    prefix="/internal",
    tags=["v1-internal-context"],
    dependencies=[Depends(require_service_token)],
)


class BuildContextRequest(BaseModel):
    attempt_id: str

    @field_validator("attempt_id")
    @classmethod
    def require_attempt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("attempt_id must not be empty")
        return value


@router.post("/tasks/{task_id}/context", response_model=ContextSnapshot)
def build_context(
    task_id: str,
    body: BuildContextRequest,
    request: Request,
    engine=Depends(get_v2_engine),
) -> ContextSnapshot:
    try:
        return ContextBuilder(
            engine,
            data_dir=getattr(request.app.state, "data_dir", None),
        ).build(task_id, attempt_id=body.attempt_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
