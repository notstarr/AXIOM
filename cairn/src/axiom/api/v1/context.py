from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.api.v1.dependencies import get_v2_engine, require_service_token
from axiom.dispatcher.context.builder import ContextBuilder, ContextSnapshot
from axiom.dispatcher.context.reader import ContextReader


router = APIRouter(
    prefix="/internal",
    tags=["v1-internal-context"],
    dependencies=[Depends(require_service_token)],
)


class BuildContextRequest(BaseModel):
    attempt_id: str
    phase: Literal["bootstrap", "reason", "explore", "generic"] = "generic"
    intent_id: str | None = None

    @field_validator("attempt_id")
    @classmethod
    def require_attempt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("attempt_id must not be empty")
        return value

    @model_validator(mode="after")
    def require_intent_for_execution_phase(self):
        if self.phase in {"bootstrap", "explore"} and not (self.intent_id or "").strip():
            raise ValueError(f"intent_id is required for {self.phase} context")
        if self.intent_id is not None:
            self.intent_id = self.intent_id.strip() or None
        return self


class ExpandContextRequest(BaseModel):
    facts: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)
    todos: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    vulnerabilities: list[str] = Field(default_factory=list)

    @field_validator("facts", "intents", "todos", "knowledge", "artifacts", "vulnerabilities")
    @classmethod
    def normalize_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def require_selection(self):
        if not any(
            (
                self.facts,
                self.intents,
                self.todos,
                self.knowledge,
                self.artifacts,
                self.vulnerabilities,
            )
        ):
            raise ValueError("at least one context entity ID is required")
        return self


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
        ).build(
            task_id,
            attempt_id=body.attempt_id,
            phase=body.phase,
            intent_id=body.intent_id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/tasks/{task_id}/context/expand")
def expand_context(
    task_id: str,
    body: ExpandContextRequest,
    engine=Depends(get_v2_engine),
):
    try:
        return ContextReader(engine).expand(task_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/tasks/{task_id}/context/intents/{intent_id}/related")
def related_intent_context(
    task_id: str,
    intent_id: str,
    engine=Depends(get_v2_engine),
):
    try:
        return ContextReader(engine).related_intent(task_id, intent_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
