from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IntentBudget:
    """The execution budget persisted on a planned Explore Intent."""

    execute_seconds: int
    conclude_seconds: int
    used_legacy_values: bool = False


@dataclass(frozen=True, slots=True)
class IntentBudgetPolicy:
    """Code-owned policy for all Explore Intent runtime budgets."""

    config: Any

    def resolve(
        self,
        *,
        execute_seconds: int | None = None,
        conclude_seconds: int | None = None,
        execute_min_seconds: int | None = None,
        execute_max_seconds: int | None = None,
        conclude_min_seconds: int | None = None,
        conclude_max_seconds: int | None = None,
    ) -> IntentBudget:
        """Resolve a budget without asking the model to choose it.

        New Reason/Plan clients omit both values and receive the configured
        code defaults. During the rolling upgrade, older workers may still
        send the fields; those values are deliberately ignored so an agent
        cannot change the runtime budget. The ``used_legacy_values`` bit lets
        callers emit a compatibility audit event without persisting
        model-owned timing choices.
        """

        config = self.config
        execute_default = int(getattr(config, "explore_execute_default_seconds", 900))
        conclude_default = int(getattr(config, "explore_conclude_default_seconds", 300))
        execute_min = int(
            execute_min_seconds
            if execute_min_seconds is not None
            else getattr(config, "explore_execute_min_seconds", 300)
        )
        execute_max = int(
            execute_max_seconds
            if execute_max_seconds is not None
            else getattr(config, "explore_execute_max_seconds", 3_600)
        )
        conclude_min = int(
            conclude_min_seconds
            if conclude_min_seconds is not None
            else getattr(config, "explore_conclude_min_seconds", 180)
        )
        conclude_max = int(
            conclude_max_seconds
            if conclude_max_seconds is not None
            else getattr(config, "explore_conclude_max_seconds", 600)
        )

        # Keep defaults valid even when an operator tightens the configured
        # range. Explicit legacy values are intentionally ignored.
        execute = min(max(execute_default, execute_min), execute_max)
        conclude = min(max(conclude_default, conclude_min), conclude_max)
        return IntentBudget(
            execute_seconds=execute,
            conclude_seconds=conclude,
            used_legacy_values=execute_seconds is not None or conclude_seconds is not None,
        )


def resolve_explore_budget(
    config: Any,
    *,
    execute_seconds: int | None = None,
    conclude_seconds: int | None = None,
    execute_min_seconds: int | None = None,
    execute_max_seconds: int | None = None,
    conclude_min_seconds: int | None = None,
    conclude_max_seconds: int | None = None,
) -> IntentBudget:
    """Compatibility helper for callers migrating to ``IntentBudgetPolicy``."""

    return IntentBudgetPolicy(config).resolve(
        execute_seconds=execute_seconds,
        conclude_seconds=conclude_seconds,
        execute_min_seconds=execute_min_seconds,
        execute_max_seconds=execute_max_seconds,
        conclude_min_seconds=conclude_min_seconds,
        conclude_max_seconds=conclude_max_seconds,
    )
