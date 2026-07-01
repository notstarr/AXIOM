from __future__ import annotations

import json
from typing import Literal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    bootstrap_worker_count: int = Field(default=1, ge=1)
    explore_worker_count: int = Field(default=2, ge=1)
    reason_worker_count: int = Field(default=1, ge=1)
    plan_timeout: int = Field(default=600, ge=1)
    plan_worker_count: int = Field(default=1, ge=1)
    crawler_active_window_seconds: int = Field(default=180, ge=15)
    crawler_flush_window_seconds: int = Field(default=45, ge=5)
    crawler_total_timeout_seconds: int = Field(default=1800, ge=60)
    openai_base_url: str = ""
    openai_model: str = ""
    proxy_url: str = ""
    proxy_enabled: bool = False
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
VulnStatus = Literal["待确认", "已验证", "忽略", "重复"]
BlackboardStatus = Literal["open", "in_progress", "resolved", "archived"]
BlackboardPriority = Literal["low", "medium", "high"]


class Vulnerability(BaseModel):
    id: str
    title: str
    type: str = ""
    severity: VulnSeverity = "info"
    description: str
    prerequisites: str = ""
    impact: str
    evidence: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    recommendation: str = ""
    affected_scope: str = ""
    status: VulnStatus = "待确认"
    source: str = "reason"
    reported_by: str
    created_at: str


class CreateVulnerabilityRequest(BaseModel):
    title: str
    type: str = ""
    severity: VulnSeverity = "info"
    description: str
    prerequisites: str = ""
    impact: str
    evidence: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    recommendation: str = ""
    affected_scope: str = ""
    status: VulnStatus = "待确认"
    source: str = "reason"
    reported_by: str

    @field_validator("title", "description", "impact", "source", "reported_by")
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

    @field_validator("status", mode="before")
    @classmethod
    def normalize_vuln_status(cls, value: str) -> str:
        text = str(value or "").strip()
        mapping = {
            "pending": "待确认",
            "confirmed": "已验证",
            "ignored": "忽略",
            "rejected": "重复",
            "待审核": "待确认",
            "已确认": "已验证",
            "已忽略": "忽略",
            "已拒绝": "重复",
        }
        return mapping.get(text.lower(), mapping.get(text, text or "待确认"))


class BlackboardItem(BaseModel):
    id: str
    title: str
    content: str
    hint: str = ""  # 一句话：何时相关，AI 按需读取
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
    hint: str = ""
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
    hint: str = ""
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


class PiSkill(BaseModel):
    id: str
    name: str
    display_name: str = ""
    description: str = ""
    content: str = ""
    enabled: bool = True
    builtin: bool = False
    source: str = "custom"
    created_at: str = ""
    updated_at: str = ""


class CreatePiSkillRequest(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    content: str = ""
    enabled: bool = True


class UpdatePiSkillRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None


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
    disabled: bool = False


class DispatchRuntimeWorkerView(BaseModel):
    name: str
    task_types: list[str]
    max_running: int = Field(ge=1)
    priority: int = Field(ge=0)
    disabled: bool = False


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
    depth_reason_max_intents: int = Field(default=5, ge=1)
    workers: list[DispatchWorkerSettings] = Field(default_factory=list)
    proxy_url: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_enabled: bool = False
    config_version: int = Field(default=1, ge=0)
    config_updated_at: str = ""
    dispatcher_applied_version: int = Field(default=0, ge=0)
    dispatcher_applied_at: str = ""
    effective_workers: list[DispatchRuntimeWorkerView] = Field(default_factory=list)
    bootstrap_worker_count: int = Field(default=1, ge=1)
    explore_worker_count: int = Field(default=2, ge=1)
    reason_worker_count: int = Field(default=1, ge=1)
    plan_timeout: int = Field(default=600, ge=1)
    plan_worker_count: int = Field(default=1, ge=1)

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


class DispatchModelItem(BaseModel):
    id: str
    context_window: int = Field(default=128000, ge=1)


class DispatchModelsResponse(BaseModel):
    models: list[DispatchModelItem] = Field(default_factory=list)
    error: str = ""


class Fact(BaseModel):
    id: str
    description: str
    archived: bool = False
    status: str | None = None
    lookup_path: str | None = None


class IntentTimeouts(BaseModel):
    execute_seconds: int = Field(ge=1)
    conclude_seconds: int = Field(ge=1)


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    timeouts: IntentTimeouts | None = None
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    archived: bool = False
    status: str | None = None
    lookup_path: str | None = None

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

    @model_validator(mode="after")
    def validate_scope_mode(self) -> "CreateProjectGroupRequest":
        if self.scope.targets and self.scope.exclude:
            raise ValueError("scope.targets and scope.exclude cannot both be set")
        return self


class UpdateProjectGroupRequest(CreateProjectGroupRequest):
    status: Literal["active", "archived"] = "active"


class ProjectMeta(BaseModel):
    id: str
    parent_project_id: str | None = None
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    depth_mode: bool = False
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
    depth_mode: bool = False
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
    targets: list[str] | None = None
    timeouts: IntentTimeouts | None = None
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

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("targets must not contain empty values")
            cleaned.append(text)
        if not cleaned:
            raise ValueError("targets must not be empty")
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


class ExploreStartRequest(BaseModel):
    target_url: str
    description: str = ""

    @field_validator("target_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("target_url must not be empty")
        return text


class ExploreStatus(BaseModel):
    project_id: str
    intent_id: str | None = None
    intent_worker: str | None = None
    status: Literal["idle", "running", "done", "failed"] = "idle"
    target_url: str = ""
    output_dir: str = ""
    sitemap_exists: bool = False
    api_list_exists: bool = False
    api_page_map_exists: bool = False
    created_at: str | None = None


class AssetRead(BaseModel):
    id: int
    project_id: str
    group_id: str = ""
    frontend_domain: str | None = None
    frontend_url: str | None = None
    backend_domain: str | None = None
    api_path: str
    api_method: str
    api_purpose: str | None = None
    auth_required: str = "unknown"
    auth_type: str | None = None
    test_status: str = "untested"
    tested_at: str | None = None
    known_vulns: str | None = None
    source_file: str | None = None
    sample_body: str | None = None
    content_type: str | None = None
    login_config_json: str | None = None
    discovered_at: str | None = None


class AssetCreate(BaseModel):
    project_id: str
    api_path: str
    api_method: str
    group_id: str = ""
    backend_domain: str = ""
    frontend_domain: str | None = None
    frontend_url: str | None = None
    api_purpose: str | None = None
    auth_required: str = "unknown"
    auth_type: str | None = None
    test_status: Literal["untested", "safe", "vulnerable", "blocked"] = "untested"
    source_file: str | None = None
    sample_body: str | None = None
    content_type: str | None = None
    login_config_json: str | None = None


class AssetUpdate(BaseModel):
    api_purpose: str | None = None
    test_status: Literal["untested", "safe", "vulnerable", "blocked"] | None = None
    auth_required: str | None = None
    auth_type: str | None = None
    known_vulns: str | None = None
    login_config_json: str | None = None


class AssetStats(BaseModel):
    total: int
    safe: int
    blocked: int
    vulnerable: int
    untested: int


AssetAuthType = Literal["cookie", "jwt", "bearer", "basic", "custom_header", "none"]


class CredentialRead(BaseModel):
    id: str
    group_id: str
    label: str = ""
    auth_type: str = "cookie"
    credentials_json: str = "{}"
    domains_json: str = "[]"
    source_task_id: str = ""
    captured_at: str = ""
    expires_at: str | None = None
    active: int = 1
    asset_id: int | None = None


class CredentialCreate(BaseModel):
    group_id: str
    label: str = ""
    auth_type: AssetAuthType = "cookie"
    credentials_json: str = "{}"
    domains_json: str = "[]"
    source_task_id: str = ""
    expires_at: str | None = None
    asset_id: int | None = None

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("group_id must not be empty")
        return text

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        return value.strip()

    @field_validator("credentials_json")
    @classmethod
    def validate_credentials_json(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("credentials_json must not be empty")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("credentials_json must be valid JSON object") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("credentials_json must be a non-empty JSON object")
        return text

    @field_validator("domains_json")
    @classmethod
    def validate_domains_json(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return "[]"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("domains_json must be valid JSON array") from exc
        if not isinstance(parsed, list):
            raise ValueError("domains_json must be a JSON array")
        return text

    @field_validator("source_task_id")
    @classmethod
    def normalize_source_task_id(cls, value: str) -> str:
        return value.strip()


class CredentialUpdate(BaseModel):
    label: str | None = None
    auth_type: AssetAuthType | None = None
    credentials_json: str | None = None
    domains_json: str | None = None
    expires_at: str | None = None
    active: bool | None = None
    asset_id: int | None = None

    @field_validator("label")
    @classmethod
    def normalize_update_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("credentials_json")
    @classmethod
    def validate_update_credentials_json(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("credentials_json must not be empty")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("credentials_json must be valid JSON object") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("credentials_json must be a non-empty JSON object")
        return text

    @field_validator("domains_json")
    @classmethod
    def validate_update_domains_json(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return "[]"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("domains_json must be valid JSON array") from exc
        if not isinstance(parsed, list):
            raise ValueError("domains_json must be a JSON array")
        return text
