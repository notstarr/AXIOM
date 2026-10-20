from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CMD_PLACEHOLDER = "{{cmd}}"

WebShellFamily = Literal["auto", "posix", "windows"]


class WebShellRequestTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = f"cmd={CMD_PLACEHOLDER}"
    query: str = ""
    # base64 包装命令，绕过对明文命令关键字的拦截
    wrap: Literal["none", "base64"] = "none"
    # 默认校验证书；仅对明确接受风险的自签目标使用 --insecure。
    verify_tls: bool = True

    @model_validator(mode="after")
    def require_placeholder(self) -> "WebShellRequestTemplate":
        if CMD_PLACEHOLDER not in self.body and CMD_PLACEHOLDER not in self.query:
            raise ValueError("request body 或 query 必须包含命令占位符 {{cmd}}")
        return self


class WebShellResponseTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["raw", "between", "regex"] = "raw"
    start: str = ""
    end: str = ""
    pattern: str = ""
    group: int = 0
    decode: Literal["none", "base64"] = "none"

    @model_validator(mode="after")
    def require_rule(self) -> "WebShellResponseTemplate":
        if self.mode == "between" and not (self.start and self.end):
            raise ValueError("between 模式需要 start 与 end 标记")
        if self.mode == "regex" and not self.pattern:
            raise ValueError("regex 模式需要 pattern")
        return self


class CreateWebShell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    family: WebShellFamily = "auto"
    request: WebShellRequestTemplate | None = None
    response: WebShellResponseTemplate | None = None
    remark: str = ""

    @field_validator("name", "url")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url 必须是 http(s) 地址")
        return value

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str) -> str:
        return value.strip()


class UpdateWebShell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    family: WebShellFamily | None = None
    request: WebShellRequestTemplate | None = None
    response: WebShellResponseTemplate | None = None
    remark: str | None = None

    @field_validator("name", "url")
    @classmethod
    def require_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("url 必须是 http(s) 地址")
        return value


class WebShellConnection(BaseModel):
    id: str
    task_id: str
    name: str
    url: str
    family: WebShellFamily
    request: WebShellRequestTemplate
    response: WebShellResponseTemplate
    status: Literal["untested", "alive", "dead"]
    last_check_at: str | None = None
    last_check_summary: str = ""
    remark: str = ""
    created_by: str
    source_intent_id: str | None = None
    created_at: str
    updated_at: str
    archived: bool = False


class ExecCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("command")
    @classmethod
    def require_command(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("command must not be empty")
        return value


class WebShellFileOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["list", "read", "write"]
    path: str
    content: str = ""

    @field_validator("path")
    @classmethod
    def require_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("path must not be empty")
        return value


class WebShellExecResult(BaseModel):
    connection_id: str
    log_id: str
    command: str
    ok: bool
    output: str
    duration_ms: int
    checked_at: str


class WebShellExecLog(BaseModel):
    id: str
    connection_id: str
    command: str
    output: str
    ok: bool
    duration_ms: int
    actor: str
    intent_id: str | None = None
    created_at: str
