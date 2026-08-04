from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from axiom.modules.phase_capabilities.schemas import CapabilityScope


AxiomToolPhase = Literal["bootstrap", "reason", "explore", "review"]
AxiomToolStatus = Literal["ready", "invalid", "missing"]
AxiomToolSource = Literal["bundled", "managed"]


class AxiomToolBinding(BaseModel):
    # ``scope`` is the new source-of-truth field.  worker_name/phase remain
    # optional compatibility projections for the retired Worker-specific API.
    scope: CapabilityScope | None = None
    worker_name: str = ""
    phase: str = ""
    effective: bool = False
    inactive_reason: str = ""

    @model_validator(mode="after")
    def migrate_legacy_phase(self) -> "AxiomToolBinding":
        if self.scope is None and self.phase in {
            "common",
            "bootstrap",
            "reason",
            "explore",
            "review",
        }:
            self.scope = self.phase
        return self


class AxiomTool(BaseModel):
    key: str
    title: str
    version: str
    description: str
    source: AxiomToolSource
    package_path: str
    entrypoint: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    supported_phases: list[AxiomToolPhase] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    package_hash: str
    status: AxiomToolStatus
    status_error: str = ""
    enabled: bool = True
    discovered_at: str
    created_at: str
    updated_at: str
    scopes: list[CapabilityScope] = Field(default_factory=list)
    bindings: list[AxiomToolBinding] = Field(default_factory=list)
    call_count: int = 0
    last_used_at: str = ""
    last_load_error: str = ""


class AxiomToolScanResult(BaseModel):
    scanned_at: str
    ready: int = 0
    invalid: int = 0
    missing: int = 0
    items: list[AxiomTool] = Field(default_factory=list)


class UpdateAxiomTool(BaseModel):
    enabled: bool


class WorkerAxiomToolBindings(BaseModel):
    bootstrap: list[str] = Field(default_factory=list)
    reason: list[str] = Field(default_factory=list)
    explore: list[str] = Field(default_factory=list)
    review: list[str] = Field(default_factory=list)

    @field_validator("bootstrap", "reason", "explore", "review")
    @classmethod
    def normalize_keys(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class WorkerAxiomToolBindingsView(WorkerAxiomToolBindings):
    worker_name: str
    effective: WorkerAxiomToolBindings = Field(default_factory=WorkerAxiomToolBindings)
    inactive_reasons: dict[str, str] = Field(default_factory=dict)


class AxiomToolBindingSelection(BaseModel):
    bindings: list[AxiomToolBinding] = Field(default_factory=list)


class WorkerOverview(BaseModel):
    name: str
    worker_type: str
    capabilities: list[AxiomToolPhase]
    max_running: int
    running: int = 0
    priority: int
    disabled: bool
    cooldown_until: str | None = None
    connection_state: Literal["ready", "disabled", "cooldown"] = "ready"
    config: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    context_window: int = 0
    thinking_enabled: bool = False
    thinking_level: str = "off"
    axiom_tool_counts: dict[str, int] = Field(default_factory=dict)
    mcp_tool_count: int = 0


class AxiomToolAuditCommand(BaseModel):
    task_id: str
    intent_id: str = ""
    reason_run_id: str = ""
    review_run_id: str = ""
    lease_id: str
    worker: str
    phase: AxiomToolPhase
    tool_key: str
    event_kind: Literal["execution", "load_error"] = "execution"
    outcome: Literal["success", "error", "blocked", "load_error"]
    duration_ms: int = Field(default=0, ge=0, le=86_400_000)
    error: str = Field(default="", max_length=1000)

    @field_validator(
        "task_id",
        "lease_id",
        "worker",
        "tool_key",
    )
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("intent_id", "reason_run_id", "review_run_id", "error")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_one_execution_owner(self) -> "AxiomToolAuditCommand":
        if sum(bool(item) for item in (self.intent_id, self.reason_run_id, self.review_run_id)) != 1:
            raise ValueError(
                "exactly one of intent_id, reason_run_id or review_run_id is required"
            )
        return self
