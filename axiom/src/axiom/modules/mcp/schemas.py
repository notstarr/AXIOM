from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


MCPFormat = Literal["auto", "json", "yaml", "toml"]


class MCPTool(BaseModel):
    id: str
    server_id: str
    name: str
    title: str = ""
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    usage: str = ""
    enabled: bool = True
    source: str = "manual"
    created_at: str
    updated_at: str


class MCPServerInput(BaseModel):
    name: str
    command: str = ""
    args: list[Any] = Field(default_factory=list)
    env: dict[str, Any] = Field(default_factory=dict)
    url: str = ""
    transport: str = ""
    disabled: bool = False
    source_format: str = "json"
    raw_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "command", "url", "transport")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_connection(self) -> "MCPServerInput":
        if not self.name:
            raise ValueError("MCP server name must not be empty")
        if bool(self.command) == bool(self.url):
            raise ValueError("MCP server requires exactly one of command or url")
        if not self.transport:
            self.transport = "stdio" if self.command else "http"
        return self


class MCPServer(BaseModel):
    id: str
    name: str
    transport: str
    command: str = ""
    args: list[Any] = Field(default_factory=list)
    env: dict[str, Any] = Field(default_factory=dict)
    url: str = ""
    source_format: str = "json"
    disabled: bool = False
    discovery_status: Literal["pending", "discovering", "ready", "error"] = "pending"
    discovery_error: str = ""
    discovered_at: str = ""
    server_info: dict[str, Any] = Field(default_factory=dict)
    instructions: str = ""
    created_at: str
    updated_at: str
    tools: list[MCPTool] = Field(default_factory=list)


class MCPImportRequest(BaseModel):
    content: str
    format: MCPFormat = "auto"
    replace: bool = False
    discover: bool = True

    @field_validator("content")
    @classmethod
    def require_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("MCP configuration must not be empty")
        return value


class MCPToolToggle(BaseModel):
    enabled: bool


class MCPWorkerBinding(BaseModel):
    enabled: bool = False
    server_ids: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)


class MCPWorkerBindingView(MCPWorkerBinding):
    worker_name: str
    worker_type: str = "pi"
    server_names: list[str] = Field(default_factory=list)
