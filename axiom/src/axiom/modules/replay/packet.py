"""Parse an HTTP packet back into something we can actually send.

The reproduction schema already forces every step to record a real request packet
(``POST /api/x HTTP/1.1`` + headers + body, never a curl line).  Replaying that
packet therefore needs the inverse operation: a target, a method, every header,
and the body — nothing invented.

Why a separate parser instead of ``httpx`` directly: the packet carries a
``Host`` header and an origin-form request line, not a URL.  ``httpx`` wants a URL.
The scheme is genuinely absent from the packet, so the choice of https-first with
an http fallback is our decision and has to be reported back to the caller —
falling back is itself evidence about the target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from axiom.modules.reproduction.schemas import MAX_PACKET_BYTES


# Same shape the schema enforces on capture; repeated here because replay also
# accepts hand-typed packets (the reviewer's free-form ``send``).
_REQUEST_LINE = re.compile(r"^([A-Z]+)\s+(\S+)\s+HTTP/(\d(?:\.\d)?)$")
_STATUS_LINE = re.compile(r"^HTTP/(\d(?:\.\d)?)\s+(\d{3})(?:\s+(.*))?$")

_COMMAND_PREFIXES = (
    "curl ",
    "$ ",
    "sudo ",
    "python",
    "httpx ",
    "wget ",
    "powershell",
    "bash ",
)

# Methods whose replay is allowed without an explicit flag.  DELETE stays opt-in
# because a verification run must not be able to destroy anything.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "TRACE"})
ALL_METHODS = frozenset({*SAFE_METHODS, "DELETE", "CONNECT"})


class PacketError(ValueError):
    """The packet cannot be replayed as written; the message says how to fix it."""


@dataclass(frozen=True)
class RequestPacket:
    method: str
    # Origin-form (``/api/x?y=1``) or absolute-form (``https://host/api/x``).
    target: str
    http_version: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    def url_for(self, scheme: str) -> str:
        """Absolute URL for ``scheme``.

        Absolute-form targets already carry their own scheme, in which case the
        packet wins — we do not rewrite what the worker actually sent.

        The authority comes from ``host_header``, not ``host``: the latter is
        port-stripped for address resolution, and rebuilding the URL from it would
        silently send a ``:8443`` request to 443.
        """
        if "://" in self.target:
            return self.target
        authority = self.host_header
        if not authority:
            raise PacketError("报文缺少 Host 头，无法确定目标地址")
        path = self.target if self.target.startswith("/") else f"/{self.target}"
        return f"{scheme}://{authority}{path}"

    @property
    def host(self) -> str:
        if "://" in self.target:
            return (urlsplit(self.target).hostname or "").lower()
        return header_value(self.headers, "host").split(":")[0].strip().lower()

    @property
    def host_header(self) -> str:
        if "://" in self.target:
            parsed = urlsplit(self.target)
            return parsed.netloc
        return header_value(self.headers, "host")

    def to_text(self) -> str:
        """Re-serialise, so the renderer shows exactly what was sent."""
        lines = [f"{self.method} {self.target} HTTP/{self.http_version}"]
        for name, value in self.headers.items():
            lines.append(f"{name}: {value}")
        head = "\n".join(lines)
        return f"{head}\n\n{self.body}" if self.body else f"{head}\n\n"


@dataclass(frozen=True)
class ResponsePacket:
    http_version: str
    status_code: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def status_line(self) -> str:
        suffix = f" {self.reason}".rstrip()
        return f"HTTP/{self.http_version} {self.status_code}{suffix}"

    def content_type(self) -> str:
        return header_value(self.headers, "content-type").split(";")[0].strip().lower()


def header_value(headers: dict[str, str], name: str) -> str:
    """Case-insensitive lookup; the packet's own casing is preserved elsewhere."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _split_head_body(text: str) -> tuple[list[str], str]:
    """Split at the first blank line, tolerating CRLF and trailing whitespace."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    head, _, body = normalized.partition("\n\n")
    return head.split("\n"), body


def parse_request(text: str) -> RequestPacket:
    """Parse a request packet. Every failure mode names what to change."""
    packet = (text or "").strip()
    if not packet:
        raise PacketError("请求报文为空")

    for prefix in _COMMAND_PREFIXES:
        if _first_line(packet).lower().startswith(prefix):
            raise PacketError(
                "这是命令，不是报文。请把它还原成等价报文："
                "请求行 + 关键 header + 空行 + body"
            )

    if len(packet.encode("utf-8")) > MAX_PACKET_BYTES:
        raise PacketError(
            f"请求报文超过 {MAX_PACKET_BYTES // 1024}KB，只保留能证明问题的关键 header 与 body"
        )

    lines, body = _split_head_body(packet)
    head = _first_line("\n".join(lines))
    match = _REQUEST_LINE.match(head)
    if match is None:
        raise PacketError(f"请求行必须形如 `POST /path?query HTTP/1.1`；当前是 `{head[:120]}`")

    method, target, version = match.groups()
    if method not in ALL_METHODS:
        raise PacketError(f"不支持的 HTTP 方法 `{method}`")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise PacketError(f"请求头格式错误（缺冒号）：`{line[:120]}`")
        headers[name.strip()] = value.strip()

    parsed = RequestPacket(
        method=method,
        target=target,
        http_version=version,
        headers=headers,
        body=body.strip("\n"),
    )
    if not parsed.host:
        raise PacketError("报文缺少 Host 头（或目标是绝对 URL），无法确定往哪发")
    return parsed


def parse_response(text: str) -> ResponsePacket:
    """Parse the recorded response so the comparison shows the same status line."""
    packet = (text or "").strip()
    if not packet:
        raise PacketError("响应报文为空")
    lines, body = _split_head_body(packet)
    head = _first_line("\n".join(lines))
    match = _STATUS_LINE.match(head)
    if match is None:
        raise PacketError(f"响应首行必须是状态行，如 `HTTP/1.1 200 OK`；当前是 `{head[:120]}`")

    version, code, reason = match.groups()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip()] = value.strip()

    return ResponsePacket(
        http_version=version,
        status_code=int(code),
        reason=(reason or "").strip(),
        headers=headers,
        body=body.strip("\n"),
    )


__all__ = [
    "ALL_METHODS",
    "PacketError",
    "RequestPacket",
    "ResponsePacket",
    "SAFE_METHODS",
    "header_value",
    "parse_request",
    "parse_response",
]
