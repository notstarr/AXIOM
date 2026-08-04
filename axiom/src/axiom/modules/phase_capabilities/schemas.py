from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


Phase = Literal["bootstrap", "reason", "explore"]
CapabilityScope = Literal["common", "bootstrap", "reason", "explore"]
CapabilityKind = Literal["skill", "mcp_tool", "axiom_tool"]
BuiltinToolSource = Literal["pi_native", "axiom_internal"]
BuiltinToolMode = Literal["execute", "conclude"]


class CapabilityScopeValues(BaseModel):
    skill: list[str] = Field(default_factory=list)
    mcp_tool: list[str] = Field(default_factory=list)
    axiom_tool: list[str] = Field(default_factory=list)

    @field_validator("skill", "mcp_tool", "axiom_tool")
    @classmethod
    def normalize_keys(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


class PhaseCapabilitiesUpdate(BaseModel):
    common: CapabilityScopeValues = Field(default_factory=CapabilityScopeValues)
    bootstrap: CapabilityScopeValues = Field(default_factory=CapabilityScopeValues)
    reason: CapabilityScopeValues = Field(default_factory=CapabilityScopeValues)
    explore: CapabilityScopeValues = Field(default_factory=CapabilityScopeValues)


class CapabilitySelection(BaseModel):
    scopes: list[CapabilityScope] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, values: list[CapabilityScope]) -> list[CapabilityScope]:
        normalized = list(dict.fromkeys(values))
        return ["common"] if "common" in normalized else normalized


class PhaseCapabilityRecord(BaseModel):
    capability_kind: CapabilityKind
    capability_key: str
    title: str = ""
    description: str = ""
    enabled: bool = True
    ready: bool = True
    status: str = "ready"
    supported_phases: list[Phase] = Field(default_factory=lambda: ["bootstrap", "reason", "explore"])
    server_id: str = ""
    server_name: str = ""
    scopes: list[CapabilityScope] = Field(default_factory=list)
    locked_scopes: list[CapabilityScope] = Field(default_factory=list)
    inactive_reasons: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BuiltinToolRecord(BaseModel):
    key: str
    title: str
    description: str
    source: BuiltinToolSource
    invocation: str
    phases: list[Phase] = Field(default_factory=list)
    availability: dict[Phase, list[BuiltinToolMode]] = Field(default_factory=dict)
    locked: bool = True


class PhaseCapabilitiesResponse(BaseModel):
    scopes: dict[str, CapabilityScopeValues] = Field(default_factory=dict)
    items: list[PhaseCapabilityRecord] = Field(default_factory=list)
    builtin_tools: list[BuiltinToolRecord] = Field(default_factory=list)


class PhaseBackend(BaseModel):
    name: str
    worker_type: str = "pi"
    model: str = ""
    context_window: int = 0
    thinking_enabled: bool = False
    thinking_level: str = "off"
    max_running: int = 1
    # Active work in this logical phase.  Backend capacity itself is shared
    # across all phases, so ``shared_running`` is tracked separately.
    running: int = 0
    shared_running: int = 0
    available: int = 0
    priority: int = 0
    disabled: bool = False
    connection_state: Literal["ready", "disabled", "cooldown"] = "ready"
    capabilities: list[Phase] = Field(default_factory=list)


class WorkerPhase(BaseModel):
    phase: Phase
    title: str
    backends: list[PhaseBackend] = Field(default_factory=list)
    running: int = 0
    capacity: int = 0
    available: int = 0


class PhaseBackendsUpdate(BaseModel):
    backends: list[str] = Field(default_factory=list)

    @field_validator("backends")
    @classmethod
    def normalize_backends(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
