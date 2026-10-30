"""Replay a recorded packet against the real target.

Two rules make this safe enough to expose to a reviewer:

* We resolve the host ourselves and refuse private/link-local/reserved addresses
  *before* connecting.  The replay service runs on the same VPS as AXIOM; without
  this it would become an internal-network scanner, which is precisely what the
  red-team worker is forbidden from being.
* The host must fall inside the Task's scope.  A reviewer verifying a result is
  not permitted to pivot somewhere the engagement never covered.

Everything the caller might need to judge the run — the scheme we actually used,
whether TLS verified, how long it took, whether the body was cut — comes back in
the result rather than being hidden.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from axiom.modules.replay.packet import (
    PacketError,
    RequestPacket,
    ResponsePacket,
    parse_response,
)


# Matches MAX_PACKET_BYTES so a replayed packet and a recorded one have the same
# ceiling; the report shows both in the same box.
MAX_RESPONSE_BYTES = 16 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 20.0
SCHEMES = ("https", "http")


class ReplayBlocked(ValueError):
    """The packet is well-formed but must not be sent. Carries a fix-it hint."""

    def __init__(self, reason: str, hint: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


@dataclass
class SendResult:
    sent: bool
    method: str
    url: str
    scheme_used: str = ""
    scheme_fell_back: bool = False
    status_code: int | None = None
    reason: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    body_truncated: bool = False
    body_bytes: int = 0
    elapsed_ms: int = 0
    tls_verified: bool = False
    error_kind: str = ""
    error_message: str = ""

    def as_response_packet(self) -> ResponsePacket | None:
        """The live answer in the same shape as the recorded one, so they diff."""
        if self.status_code is None:
            return None
        return ResponsePacket(
            http_version="1.1",
            status_code=self.status_code,
            reason=self.reason,
            headers=self.headers,
            body=self.body,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "method": self.method,
            "url": self.url,
            "scheme_used": self.scheme_used,
            "scheme_fell_back": self.scheme_fell_back,
            "status_code": self.status_code,
            "reason": self.reason,
            "headers": self.headers,
            "body": self.body,
            "body_truncated": self.body_truncated,
            "body_bytes": self.body_bytes,
            "elapsed_ms": self.elapsed_ms,
            "tls_verified": self.tls_verified,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
        }


def _is_blocked_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def resolve_addresses(host: str, port: int) -> list[str]:
    """Every A/AAAA record, so one private answer among public ones still blocks."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ReplayBlocked(
            f"无法解析 {host}：{exc}",
            "确认域名仍存在、DNS 可达；已失效的资产无法复核",
        ) from exc
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def assert_public_target(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Refuse the whole host if *any* answer is non-public."""
    if allow_private:
        return []
    addresses = resolve_addresses(host, port)
    if not addresses:
        raise ReplayBlocked(f"{host} 没有解析到任何地址", "已失效的资产无法复核")
    blocked = [item for item in addresses if _is_blocked_address(item)]
    if blocked:
        raise ReplayBlocked(
            f"目标 {host} 解析到内网地址 {', '.join(blocked)}，重放器不出内网",
            "该成果如需复核，请沿已登记的立足点回放："
            "axiom webshell list → axiom webshell exec <id> \"curl -i ...\"",
        )
    return addresses


def scope_allows(scope: dict[str, Any] | None, host: str) -> tuple[bool, str]:
    """Reuse the browser broker's matcher so replay and bsk agree on scope."""
    if not scope:
        return True, ""
    from axiom.dispatcher.browser_broker import normalize_scope_target, scope_entry_matches_host

    def as_host(value: object) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        try:
            return normalize_scope_target(raw)
        except Exception:
            return raw.split("/")[0].split(":")[0]

    excluded = [as_host(item) for item in scope.get("exclude", []) if as_host(item)]
    for entry in excluded:
        if scope_entry_matches_host(entry, host):
            return False, f"命中 scope 排除项 `{entry}`"
    targets = [as_host(item) for item in scope.get("targets", []) if as_host(item)]
    if not targets:
        return True, ""
    for entry in targets:
        if scope_entry_matches_host(entry, host):
            return True, ""
    return False, f"`{host}` 不在该任务 scope 内（targets: {', '.join(targets[:5])}）"


def send(
    packet: RequestPacket,
    *,
    scope: dict[str, Any] | None = None,
    follow_redirects: bool = False,
    allow_delete: bool = False,
    allow_private: bool = False,
) -> SendResult:
    """Send the packet once. Blocked packets raise; network failures do not."""
    if packet.method == "DELETE" and not allow_delete:
        raise ReplayBlocked(
            "DELETE 默认不重放",
            "复核不应产生不可逆副作用；确需验证请加 --allow-delete",
        )
    if packet.method == "CONNECT":
        raise ReplayBlocked("CONNECT 不用于重放", "这是代理隧道方法，不是业务请求")

    host = packet.host
    allowed, why = scope_allows(scope, host)
    if not allowed:
        raise ReplayBlocked(why, "该成果的目标必须在任务 scope 内才能复核")

    port = 443
    if "://" in packet.target:
        from urllib.parse import urlsplit

        parsed = urlsplit(packet.target)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    else:
        host_header = packet.host_header
        if ":" in host_header:
            try:
                port = int(host_header.rsplit(":", 1)[1])
            except ValueError:
                port = 443

    assert_public_target(host, port, allow_private=allow_private)

    last_error: Exception | None = None
    # The recorded packet carries no scheme, so finding the one the target actually
    # speaks is part of the job.  A plaintext service behind a name gives a TLS
    # error on https; refusing to fall back would make every such target look
    # unreachable.  The fallback is never silent — ``scheme_fell_back`` is reported
    # and the screenshot states which scheme was used.
    for index, scheme in enumerate(SCHEMES):
        url = packet.url_for(scheme)
        result = _attempt(
            packet,
            url,
            scheme,
            fell_back=index > 0,
            follow_redirects=follow_redirects,
        )
        if result.sent:
            return result
        last_error = RuntimeError(result.error_message or "unknown")
        if result.error_kind == "timeout":
            # A timeout is a real answer about the target; retrying the other scheme
            # would double the wait for no new information.
            return result

    assert last_error is not None
    return SendResult(sent=False, method=packet.method, url=packet.url_for("https"),
                      error_kind="unreachable", error_message=str(last_error))


def _attempt(
    packet: RequestPacket,
    url: str,
    scheme: str,
    *,
    fell_back: bool,
    follow_redirects: bool,
) -> SendResult:
    headers = _headers_for_send(packet)
    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            follow_redirects=follow_redirects,
            verify=False,
            trust_env=False,
        ) as client:
            response = client.request(
                packet.method,
                url,
                headers=headers,
                content=packet.body.encode("utf-8") if packet.body else None,
            )
            raw = response.content
    except httpx.ConnectTimeout:
        return _failure(packet, url, scheme, fell_back, "timeout", f"连接超时（{CONNECT_TIMEOUT_SECONDS:.0f}s）")
    except httpx.ReadTimeout:
        return _failure(packet, url, scheme, fell_back, "timeout", f"读取超时（{READ_TIMEOUT_SECONDS:.0f}s）")
    except httpx.ConnectError as exc:
        text = str(exc)
        kind = "tls" if any(word in text.lower() for word in ("ssl", "tls", "certificate")) else "connect"
        return _failure(packet, url, scheme, fell_back, kind, text[:300])
    except httpx.HTTPError as exc:
        return _failure(packet, url, scheme, fell_back, "http", str(exc)[:300])

    elapsed_ms = int((time.monotonic() - started) * 1000)
    body_bytes = len(raw)
    truncated = body_bytes > MAX_RESPONSE_BYTES
    if truncated:
        raw = raw[:MAX_RESPONSE_BYTES]
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        body = raw.decode("utf-8", "replace")

    return SendResult(
        sent=True,
        method=packet.method,
        url=url,
        scheme_used=scheme,
        scheme_fell_back=fell_back,
        status_code=response.status_code,
        reason=response.reason_phrase,
        headers={name: value for name, value in response.headers.items()},
        body=body,
        body_truncated=truncated,
        body_bytes=body_bytes,
        elapsed_ms=elapsed_ms,
        tls_verified=False,
    )


def _headers_for_send(packet: RequestPacket) -> dict[str, str]:
    """Drop only the headers httpx must recompute; keep every credential."""
    headers: dict[str, str] = {}
    for name, value in packet.headers.items():
        # httpx sets these; forwarding a stale one sends a wrong-length body.
        if name.lower() in {"content-length", "host", "connection", "transfer-encoding"}:
            continue
        headers[name] = value
    return headers


def _failure(
    packet: RequestPacket,
    url: str,
    scheme: str,
    fell_back: bool,
    kind: str,
    message: str,
) -> SendResult:
    return SendResult(
        sent=False,
        method=packet.method,
        url=url,
        scheme_used=scheme,
        scheme_fell_back=fell_back,
        error_kind=kind,
        error_message=message,
    )


def recorded_response(text: str) -> ResponsePacket | None:
    """The reported response, or ``None`` when the worker did not record one."""
    try:
        return parse_response(text)
    except PacketError:
        return None


__all__ = [
    "MAX_RESPONSE_BYTES",
    "ReplayBlocked",
    "SendResult",
    "assert_public_target",
    "recorded_response",
    "resolve_addresses",
    "scope_allows",
    "send",
]
