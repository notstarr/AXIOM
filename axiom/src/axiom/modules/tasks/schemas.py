from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DEFAULT_GOAL = "对该系统进行渗透测试，并挖掘10个高置信度的漏洞"


class Origin(BaseModel):
    host: str
    scheme: Literal["http", "https"] | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    original_input: str


class TaskScope(BaseModel):
    enabled: bool = True
    targets: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("targets", "exclude")
    @classmethod
    def normalize_entries(cls, values: list[str]) -> list[str]:
        from axiom.modules.tasks.origin import normalize_scope_target

        return list(
            dict.fromkeys(normalize_scope_target(value) for value in values if value.strip())
        )

    @model_validator(mode="after")
    def exclude_wins(self) -> "TaskScope":
        self.targets = [target for target in self.targets if target not in set(self.exclude)]
        return self


class CreateTask(BaseModel):
    project_id: str
    title: str
    origin: str
    goal: str = DEFAULT_GOAL
    hints: list[str] = Field(default_factory=list)
    scope: TaskScope | None = None
    worker_pool: list[str] = Field(default_factory=list)
    guard_enabled: bool = False
    external_report_enabled: bool = False

    @field_validator("project_id", "title", "origin", "goal")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("hints", "worker_pool")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class ActiveReasonRun(BaseModel):
    id: str
    status: Literal["pending", "claimed", "running", "concluding"]
    trigger: str
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    worker: str | None = None
    lease_id: str | None = None
    settings_version: int
    execute_timeout_seconds: int
    conclude_timeout_seconds: int
    last_heartbeat_at: str | None = None
    activity_kind: str = ""
    activity_summary: str = ""
    activity_tool: str = ""
    activity_started_at: str | None = None
    activity_updated_at: str | None = None
    created_at: str


class Task(BaseModel):
    id: str
    project_id: str
    title: str
    origin: Origin
    goal: str
    status: Literal["running", "stopped", "completed", "failed"]
    phase: Literal["bootstrap", "reason", "explore", "waiting", "none"]
    scope: TaskScope
    worker_pool: list[str]
    guard_enabled: bool
    external_report_enabled: bool
    legacy_readonly: bool
    created_at: str
    updated_at: str
    completed_at: str | None = None
    fact_count: int = 0
    intent_count: int = 0
    working_intent_count: int = 0
    hint_count: int = 0
    todo_count: int = 0
    vulnerability_count: int = 0
    reason_worker: str | None = None
    reason_heartbeat_at: str | None = None
    reason_activity_kind: str = ""
    reason_activity_summary: str = ""
    reason_activity_tool: str = ""
    reason_activity_started_at: str | None = None
    reason_activity_updated_at: str | None = None
    active_reason_run: ActiveReasonRun | None = None


class TaskHint(BaseModel):
    id: str
    task_id: str
    content: str
    creator: str
    created_at: str
    archived: bool = False


class UpdateTask(BaseModel):
    title: str | None = None
    goal: str | None = None
    scope: TaskScope | None = None
    worker_pool: list[str] | None = None
    guard_enabled: bool | None = None
    external_report_enabled: bool | None = None

    @field_validator("title", "goal")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("worker_pool")
    @classmethod
    def normalize_workers(cls, values: list[str] | None) -> list[str] | None:
        return (
            list(dict.fromkeys(value.strip() for value in values if value.strip()))
            if values is not None
            else None
        )


class CreateTaskHint(BaseModel):
    content: str
    creator: str = "human"

    @field_validator("content", "creator")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class UpdateTaskHint(BaseModel):
    content: str | None = None
    archived: bool | None = None

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("content must not be empty")
        return value
