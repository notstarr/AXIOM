from __future__ import annotations

from fastapi import APIRouter, Depends

from axiom.api.dependencies import get_container
from axiom.composition import Container
from axiom.modules.guard.policy import HARD_BLOCK_RULES, SYSTEM_PROMPT, WHITELIST_BASH_VERBS, WHITELIST_TOOLS
from axiom.modules.guard.schemas import GuardDecision, GuardDefaults, GuardRequest


router = APIRouter(tags=["guard"])


@router.post("/internal/guard/pre", response_model=GuardDecision)
def guard_pre(
    body: GuardRequest,
    container: Container = Depends(get_container),
) -> GuardDecision:
    return container.guard.decide(body)


@router.get("/guard/defaults", response_model=GuardDefaults)
def guard_defaults() -> GuardDefaults:
    return GuardDefaults(
        whitelist_tools=WHITELIST_TOOLS,
        whitelist_bash_verbs=WHITELIST_BASH_VERBS,
        hard_block_rules=HARD_BLOCK_RULES,
        system_prompt=SYSTEM_PROMPT,
    )
