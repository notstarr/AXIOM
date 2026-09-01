from __future__ import annotations

from axiom.modules.execution.intent_budget import IntentBudgetPolicy
from axiom.modules.settings.schemas import RuntimeConfig


def test_intent_budget_policy_assigns_defaults_when_fields_are_omitted() -> None:
    config = RuntimeConfig()

    budget = IntentBudgetPolicy(config).resolve()

    assert budget.execute_seconds == 900
    assert budget.conclude_seconds == 300
    assert budget.used_legacy_values is False


def test_intent_budget_policy_ignores_legacy_values_and_honors_snapshot_bounds() -> None:
    config = RuntimeConfig(
        explore_execute_max_seconds=600,
        explore_conclude_max_seconds=240,
    )

    budget = IntentBudgetPolicy(config).resolve(
        execute_seconds=1,
        conclude_seconds=7_200,
    )

    assert budget.execute_seconds == 600
    assert budget.conclude_seconds == 240
    assert budget.used_legacy_values is True
