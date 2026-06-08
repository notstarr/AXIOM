from __future__ import annotations

from typing import Literal
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    bootstrap_worker_count: int = Field(default=1, ge=1)
    explore_worker_count: int = Field(default=2, ge=1)
    reason_worker_count: int = Field(default=1, ge=1)
    openai_base_url: str = ""
    openai_model: str = ""
    proxy_url: str = ""
    proxy_bootstrap_enabled: bool = False
    proxy_explore_enabled: bool = False
    domain_blacklist: list[str] = Field(default_factory=list)

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        if not text.startswith("socks5://"):
            raise ValueError("proxy_url must start with socks5://")
        return text

    @field_validator("openai_base_url")
    @classmethod
    def validate_openai_base_url(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if not text:
            return ""
        if not (text.startswith("http://") or text.startswith("https://")):
            raise ValueError("openai_base_url must start with http:// or https://")
        return text

    @field_validator("openai_model")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("domain_blacklist")
    @classmethod
    def validate_domain_blacklist(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            item = raw.strip().lower()
            if not item:
                continue
            item = item.removeprefix("http://").removeprefix("https://")
            item = item.split("/", 1)[0].split(":", 1)[0].strip(".")
            if not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            normalized.append(item)
        return normalized


class ProviderKeyMeta(BaseModel):
    provider: str
    has_key: bool
    masked_key: str
    is_active: bool
    updated_at: str | None = None
    updated_by: str | None = None


class ProviderKeysResponse(BaseModel):
    items: list[ProviderKeyMeta]


class UpdateProviderKeyRequest(BaseModel):
    api_key: str
    updated_by: str = "human"

    @field_validator("api_key", "updated_by")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ProjectScope(BaseModel):
    targets: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    notes: str = ""

    @field_validator("targets", "exclude")
    @classmethod
    def validate_scope_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            text = raw.strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        return cleaned

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: str) -> str:
        return value.strip()


class TaskEvent(BaseModel):
    id: int
    project_id: str
    task_type: str
    phase: str
    worker: str
    level: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class TaskEventCreateRequest(BaseModel):
    task_type: str
    phase: str
    worker: str
    level: str = "info"
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_type", "phase", "worker", "level", "message")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


VulnSeverity = Literal["info", "low", "medium", "high", "critical"]
VulnStatus = Literal["confirmed", "pending", "rejected", "ignored"]
BlackboardStatus = Literal["open", "in_progress", "resolved", "archived"]
BlackboardPriority = Literal["low", "medium", "high"]


class Vulnerability(BaseModel):
    id: str
    title: str
    type: str
    severity: VulnSeverity
    description: str
    impact: str
    evidence: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    recommendation: str
    affected_scope: str
    status: VulnStatus = "confirmed"
    source: str = "reason"
    reported_by: str
    created_at: str


class CreateVulnerabilityRequest(BaseModel):
    title: str
    type: str
    severity: VulnSeverity
    description: str
    impact: str
    evidence: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    recommendation: str
    affected_scope: str
    status: VulnStatus = "confirmed"
    source: str = "reason"
    reported_by: str

    @field_validator(
        "title",
        "type",
        "description",
        "impact",
        "recommendation",
        "affected_scope",
        "source",
        "reported_by",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("evidence", "reproduction_steps")
    @classmethod
    def validate_string_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = item.strip()
            if text:
                cleaned.append(text)
        return cleaned


class BlackboardItem(BaseModel):
    id: str
    title: str
    content: str
    category: str = "general"
    status: BlackboardStatus = "open"
    priority: BlackboardPriority = "medium"
    related_fact_id: str | None = None
    related_intent_id: str | None = None
    related_vuln_id: str | None = None
    creator: str
    updated_by: str
    created_at: str
    updated_at: str


class CreateBlackboardItemRequest(BaseModel):
    title: str
    content: str
    category: str = "general"
    status: BlackboardStatus = "open"
    priority: BlackboardPriority = "medium"
    related_fact_id: str | None = None
    related_intent_id: str | None = None
    related_vuln_id: str | None = None
    creator: str

    @field_validator("title", "content", "category", "creator")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateBlackboardItemRequest(BaseModel):
    title: str
    content: str
    category: str = "general"
    status: BlackboardStatus
    priority: BlackboardPriority = "medium"
    related_fact_id: str | None = None
    related_intent_id: str | None = None
    related_vuln_id: str | None = None
    updated_by: str

    @field_validator("title", "content", "category", "updated_by")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("related_fact_id", "related_intent_id", "related_vuln_id")
    @classmethod
    def validate_optional_relation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class BlackboardBatchStatusUpdateRequest(BaseModel):
    ids: list[str] = Field(min_length=1)
    status: BlackboardStatus
    updated_by: str

    @field_validator("updated_by")
    @classmethod
    def validate_updated_by(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if text:
                cleaned.append(text)
        if not cleaned:
            raise ValueError("ids must not be empty")
        return cleaned


class PiTool(BaseModel):
    id: str
    name: str
    display_name: str
    description: str = ""
    enabled: bool = True
    builtin: bool = False
    source: str = "custom"
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = {"populate_by_name": True}


class CreatePiToolRequest(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    enabled: bool = True
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    config: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("display_name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class UpdatePiToolRequest(BaseModel):
    display_name: str = ""
    description: str = ""
    enabled: bool = True
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    config: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("display_name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class DispatchWorkerSettings(BaseModel):
    name: str
    task_types: list[str]
    max_running: int = Field(ge=1)
    priority: int = Field(ge=0)
    pi_model: str
    pi_base_url: str
    pi_provider_api: str
    pi_model_context_window: str = ""
    pi_api_key: str = ""
    pi_api_key_masked: str = ""


class DispatchSettings(BaseModel):
    runtime_interval: int = Field(default=3, ge=1)
    runtime_max_workers: int = Field(default=8, ge=1)
    runtime_max_running_projects: int = Field(default=3, ge=1)
    runtime_max_project_workers: int = Field(default=4, ge=1)
    runtime_healthcheck_timeout: int = Field(default=20, ge=1)
    runtime_worker_healthcheck: Literal["startup_and_task", "startup_only", "disabled"] = "startup_only"
    runtime_prompt_group: str = "default"
    bootstrap_timeout: int = Field(default=300, ge=1)
    bootstrap_conclude_timeout: int = Field(default=90, ge=1)
    explore_timeout: int = Field(default=300, ge=1)
    explore_conclude_timeout: int = Field(default=90, ge=1)
    reason_timeout: int = Field(default=300, ge=1)
    reason_max_intents: int = Field(default=2, ge=1)
    workers: list[DispatchWorkerSettings] = Field(default_factory=list)
    proxy_url: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_bootstrap_enabled: bool = False
    proxy_explore_enabled: bool = False

    @field_validator("runtime_prompt_group")
    @classmethod
    def validate_prompt_group(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class PiConnectionTestRequest(BaseModel):
    worker_name: str = ""
    pi_base_url: str
    pi_model: str
    pi_provider_api: str = "openai-completions"
    pi_api_key: str = ""

    @field_validator("pi_base_url")
    @classmethod
    def validate_pi_base_url(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if not text:
            raise ValueError("must not be empty")
        if not (text.startswith("http://") or text.startswith("https://")):
            raise ValueError("base url must start with http:// or https://")
        return text

    @field_validator("worker_name", "pi_api_key")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("pi_model", "pi_provider_api")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class PiConnectionTestResponse(BaseModel):
    ok: bool
    message: str


class Fact(BaseModel):
    id: str
    description: str


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectGroupMeta(BaseModel):
    id: str
    title: str
    description: str = ""
    scope: ProjectScope = Field(default_factory=ProjectScope)
    status: Literal["active", "archived"] = "active"
    created_at: str


class ProjectGroupSummary(ProjectGroupMeta):
    task_count: int = 0
    active_task_count: int = 0
    stopped_task_count: int = 0
    completed_task_count: int = 0
    vuln_count: int = 0
    blackboard_count: int = 0


class CreateProjectGroupRequest(BaseModel):
    title: str
    description: str = ""
    scope: ProjectScope = Field(default_factory=ProjectScope)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return value.strip()


class UpdateProjectGroupRequest(CreateProjectGroupRequest):
    status: Literal["active", "archived"] = "active"


class ProjectMeta(BaseModel):
    id: str
    parent_project_id: str | None = None
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    vulns: list[Vulnerability] = Field(default_factory=list)
    blackboard: list[BlackboardItem] = Field(default_factory=list)


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    parent_project_id: str | None = None
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("parent_project_id")
    @classmethod
    def validate_parent_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
