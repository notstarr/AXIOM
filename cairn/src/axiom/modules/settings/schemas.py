from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class RuntimeConfig(BaseModel):
    interval_seconds: int = Field(default=1, gt=0)
    max_workers: int = Field(default=4, gt=0)
    max_running_tasks: int = Field(default=2, gt=0)
    max_task_workers: int = Field(default=2, gt=0)
    bootstrap_limit: int = Field(default=1, gt=0)
    reason_limit: int = Field(default=1, gt=0)
    explore_limit: int = Field(default=2, gt=0)
    crawler_enabled: bool = False

    @model_validator(mode="after")
    def validate_limits(self) -> "RuntimeConfig":
        if self.max_task_workers > self.max_workers:
            raise ValueError("max_task_workers cannot exceed max_workers")
        return self


class RuntimeSettings(BaseModel):
    config: RuntimeConfig
    version: int
    updated_by: str
    updated_at: str
