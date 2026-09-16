from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.execution.schemas import LeaseCommand
from axiom.modules.interventions.schemas import CreateIntervention, Intervention, ResolveIntervention


router = APIRouter(tags=["interventions"])


@router.post("/tasks/{task_id}/intents/{intent_id}/interventions", response_model=Intervention)
def create_intervention(task_id: str, intent_id: str, body: CreateIntervention, container: Container = Depends(get_container)) -> Intervention:
    return container.interventions.create(task_id, intent_id, body)


@router.post("/dispatch/tasks/{task_id}/intents/{intent_id}/interventions/claim", response_model=list[Intervention])
def claim_interventions(task_id: str, intent_id: str, body: LeaseCommand, container: Container = Depends(get_container)) -> list[Intervention]:
    return container.interventions.claim(task_id, intent_id, body.lease_id)


@router.post("/dispatch/tasks/{task_id}/intents/{intent_id}/interventions/{intervention_id}/resolve", response_model=Intervention)
def resolve_intervention(task_id: str, intent_id: str, intervention_id: str, body: ResolveIntervention, lease_id: str = Query(...), container: Container = Depends(get_container)) -> Intervention:
    return container.interventions.resolve(task_id, intent_id, lease_id, intervention_id, body)


@router.post("/dispatch/tasks/{task_id}/intents/{intent_id}/interventions/expire", status_code=204)
def expire_interventions(task_id: str, intent_id: str, body: LeaseCommand, container: Container = Depends(get_container)) -> None:
    container.interventions.expire(task_id, intent_id, body.lease_id)
