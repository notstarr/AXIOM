"""Structured reproduction steps captured in-situ by Explore.

A finding or a red-team Result is only as useful as the operator's ability to walk it
again from zero.  The prose was historically a one-line summary plus a copy-pasted
curl command, which is machine-shaped evidence rendered to a human reader — the
report consumer has to retype it into a proxy to see what actually happened.

So Explore submits the real thing at the moment it plays the exploit: for each step
the HTTP request packet it actually sent, the response it actually got back, and what
that step proves.  The report skeleton around those steps is owned by code
(``axiom.modules.reports``), so a worker can supply content but never layout.

The packet rules are strict on purpose.  ``request`` must be a standard HTTP request
packet, not a curl/shell command, because (a) the report shows it to a human who will
paste it into a proxy, and (b) a curl line silently drops headers, which are usually
exactly what the vulnerability hinges on.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# A single request/response packet.  Real packets in this platform are a few KB; the
# cap stops a worker from dumping a 40 MB download into a JSONB column.
MAX_PACKET_BYTES = 16 * 1024
# Whole reproduction (all steps + prose).  Twelve steps of realistic packets fit well
# inside this; going over means the worker is pasting whole responses instead of the
# fragment that proves the point.
MAX_REPRODUCTION_BYTES = 64 * 1024
MAX_STEPS = 12

# ``METHOD target HTTP/1.1`` — the target may be an origin-form path or an absolute
# URL (a proxy-style request line is still a valid packet).
_REQUEST_LINE = re.compile(r"^[A-Z]+ \S+ HTTP/\d(?:\.\d)?$")
_STATUS_LINE = re.compile(r"^HTTP/\d(?:\.\d)? \d{3}\b")

# Openers that mean "this is a command, not a packet".  Kept as a tuple rather than a
# regex so the error message can name what was found.
_COMMAND_PREFIXES = ("curl ", "$ ", "sudo ", "python", "httpx ", "wget ", "powershell", "bash ")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


class ReproStep(BaseModel):
    """One ordered step: the real request, the real response, and what it proves."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="这一步在做什么（自然语言，给读者看的一句话）")
    request: str = Field(description="该步真实发出的 HTTP 请求报文（不是 curl/命令）")
    response: str = Field(description="该步真实返回的 HTTP 响应（状态行 + 关键内容）")
    proves: str = Field(description="这一步证明了什么")

    @field_validator("title", "proves")
    @classmethod
    def require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空：每一步都要说清在做什么、证明了什么")
        return value

    @field_validator("request")
    @classmethod
    def require_packet(cls, value: str) -> str:
        packet = value.strip()
        if not packet:
            raise ValueError("请求报文不能为空")
        head = _first_line(packet)
        lowered = head.lower()
        for prefix in _COMMAND_PREFIXES:
            if lowered.startswith(prefix):
                raise ValueError(
                    "request 必须是标准 HTTP 请求报文，不能是 curl/shell 命令"
                    "（把命令还原成等价报文：请求行 + 关键 header + 空行 + body）"
                )
        if not _REQUEST_LINE.match(head):
            raise ValueError(
                "request 首行必须形如 `POST /path?query HTTP/1.1`；"
                f"当前首行是 `{head[:120]}`"
            )
        if _utf8_len(packet) > MAX_PACKET_BYTES:
            raise ValueError(
                f"单条请求报文超过 {MAX_PACKET_BYTES // 1024}KB，"
                "只保留能证明问题的关键 header 与 body 片段"
            )
        return packet

    @field_validator("response")
    @classmethod
    def require_status_line(cls, value: str) -> str:
        packet = value.strip()
        if not packet:
            raise ValueError(
                "响应不能为空：至少给出状态行与能证明问题的关键内容"
                "（不要用 ... 省略掉证明点）"
            )
        head = _first_line(packet)
        if not _STATUS_LINE.match(head):
            raise ValueError(
                "response 首行必须是 HTTP 状态行，如 `HTTP/1.1 200 OK`；"
                f"当前首行是 `{head[:120]}`"
            )
        if _utf8_len(packet) > MAX_PACKET_BYTES:
            raise ValueError(
                f"单条响应报文超过 {MAX_PACKET_BYTES // 1024}KB，"
                "只保留能证明问题的关键片段（回显 / token / 报错）"
            )
        return packet


class Reproduction(BaseModel):
    """The ordered exploit path plus the local context that the reporting worker owns.

    ``chain_input`` / ``chain_gain`` are deliberately *local*: what identity and reach
    this step started with, and what it added.  The global attack path (how the task
    got here at all) is assembled from the graph by ``axiom.modules.reports.path`` —
    a worker only ever sees its own slice of a long engagement and would have to
    invent the rest.
    """

    model_config = ConfigDict(extra="forbid")

    entry_point: str = Field(description="触发漏洞的关键接口（方法 + 路径），如 POST /user/validate/otp")
    prerequisites: list[str] = Field(
        default_factory=list,
        description="本环前置：需要的账号/角色/会话/网络可达性。含测试账号时写清如何注册",
    )
    chain_input: str = Field(
        default="",
        description="本环开始时的身份与权限（如「已持有 v004 面板 token，可访问 172.21.x」）",
    )
    chain_gain: str = Field(
        default="",
        description="本环新增的能力（如「拿到 192.168.0.95 的 shell，可执行命令」）",
    )
    steps: list[ReproStep] = Field(description=f"有序复现步骤，1..{MAX_STEPS} 步")
    notes: str = Field(default="", description="补充说明（可选）")

    @field_validator("entry_point")
    @classmethod
    def require_entry_point(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("入口点不能为空（方法 + 路径）")
        return value

    @field_validator("prerequisites")
    @classmethod
    def clean_prerequisites(cls, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item.strip()]
        return list(dict.fromkeys(cleaned))

    @field_validator("chain_input", "chain_gain", "notes")
    @classmethod
    def strip_optional(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("steps")
    @classmethod
    def require_steps(cls, steps: list[ReproStep]) -> list[ReproStep]:
        if not steps:
            raise ValueError("至少要有一个复现步骤；没有可复现路径就不算已确认")
        if len(steps) > MAX_STEPS:
            raise ValueError(
                f"复现步骤最多 {MAX_STEPS} 步，当前 {len(steps)} 步；"
                "把同质步骤合并，或只保留能从入口走到证实的那条路径"
            )
        return steps

    def byte_size(self) -> int:
        return _utf8_len(self.model_dump_json())

    def check_size(self) -> None:
        """Reject an oversized reproduction with a message naming the actual size."""
        size = self.byte_size()
        if size > MAX_REPRODUCTION_BYTES:
            raise ValueError(
                f"整份复现 {size // 1024}KB 超过上限 "
                f"{MAX_REPRODUCTION_BYTES // 1024}KB；请截短响应内容"
            )

    @model_validator(mode="after")
    def enforce_document_size(self) -> "Reproduction":
        # Enforced on the model, not only in the CLI parser, so every construction
        # path (API, sandbox replay, tests) hits the same limit.
        self.check_size()
        return self
