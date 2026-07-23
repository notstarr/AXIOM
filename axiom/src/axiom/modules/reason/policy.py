from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReasonTriggerPolicy:
    """Small, explicit policy for batching new evidence into Reason runs."""

    batch_size: int = 2
    low_watermark: int = 1
    max_wait_seconds: int = 180

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.low_watermark < 0:
            raise ValueError("low_watermark must not be negative")
        if self.max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")

    def should_trigger(
        self,
        *,
        unplanned_facts: int,
        open_explore_intents: int,
        oldest_fact_age_seconds: float,
    ) -> bool:
        if unplanned_facts <= 0:
            return False
        return (
            unplanned_facts >= self.batch_size
            or open_explore_intents < self.low_watermark
            or oldest_fact_age_seconds >= self.max_wait_seconds
        )
