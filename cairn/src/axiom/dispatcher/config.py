from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from axiom.server.dispatch_config_store import load_dispatch_config_snapshot


TaskType = Literal["reason", "explore", "bootstrap", "plan"]
WorkerType = Literal["pi"]
WorkerHealthcheckMode = Literal["startup_and_task", "startup_only", "disabled"]

WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "pi": (
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
}

DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": ("{graph_yaml}", "{fact_ids}", "{open_intents}", "{max_intents}", "{blackboard}"),
    "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}", "{blackboard}"),
    "explore_conclude.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
}


class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)
    depth_max_intents: int = Field(gt=0, default=5)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class PlanTaskConfig(BaseModel):
    timeout: int = Field(gt=0)


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    plan: PlanTaskConfig = PlanTaskConfig(timeout=600)
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig


class RuntimeConfig(BaseModel):
    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    worker_healthcheck: WorkerHealthcheckMode = "startup_only"
    prompt_group: str = Field(min_length=1)


class PhaseLimitsConfig(BaseModel):
    bootstrap: int = Field(default=1, gt=0)
    plan: int = Field(default=1, gt=0)
    explore: int = Field(default=1, gt=0)
    reason: int = Field(default=1, gt=0)

    def for_task(self, task_type: TaskType) -> int:
        return getattr(self, task_type)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: WorkerType
    task_types: list[TaskType]
    max_running: int = Field(gt=0)
    priority: int = Field(ge=0)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        required = WORKER_ENV_KEYS[self.type]
        missing = [key for key in required if not self.env.get(key)]
        if missing:
            raise ValueError(f"worker {self.name} missing env keys: {', '.join(missing)}")
        if self.type == "pi":
            _validate_optional_positive_int_env(self.name, self.env, "PI_MODEL_CONTEXT_WINDOW")
        return self


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server: str
    runtime: RuntimeConfig
    tasks: TasksConfig
    common_env: dict[str, str] = Field(default_factory=dict)
    workers: list[WorkerConfig]
    phase_limits: PhaseLimitsConfig = Field(default_factory=PhaseLimitsConfig)

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            worker_copy["env"] = {**common_env, **worker_env}
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        if not self.workers:
            raise ValueError("workers must not be empty")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        return self

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        snapshot = load_dispatch_config_snapshot(path)
        config = cls.model_validate(snapshot.config_data)
        apply_runtime_overrides(config)
        validate_prompt_resources(config.runtime.prompt_group)
        return config


def apply_runtime_overrides(config: DispatchConfig) -> None:
    config.runtime.max_project_workers = max(1, min(config.runtime.max_project_workers, config.runtime.max_workers))
    config.phase_limits.bootstrap = max(1, config.phase_limits.bootstrap)
    config.phase_limits.explore = max(1, config.phase_limits.explore)
    config.phase_limits.reason = max(1, config.phase_limits.reason)


def _validate_optional_positive_int_env(worker_name: str, env: dict[str, str], key: str) -> None:
    value = env.get(key)
    if value is None or not value.strip():
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"worker {worker_name} env {key} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"worker {worker_name} env {key} must be greater than 0")


def validate_prompt_resources(prompt_group: str) -> None:
    prompts_dir = resources.files("axiom.dispatcher.prompts")
    group_dir = prompts_dir.joinpath(prompt_group)
    if not group_dir.is_dir():
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = DEFAULT_PROMPT_REQUIRED_TOKENS
    for name, tokens in required_tokens.items():
        try:
            content = group_dir.joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}")
