from __future__ import annotations

import ipaddress
import re
from typing import NamedTuple
from urllib.parse import urlsplit

from axiom.modules.tasks.schemas import Origin
from axiom.shared.errors import InvalidOriginError


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOGICAL_TARGET_PREFIXES = (
    "app:",
    "artifact:",
    "component:",
    "fact:",
    "file:",
    "intent:",
    "miniapp:",
    "module:",
    "package:",
    "page:",
    "route:",
    "screen:",
    "todo:",
)


class NetworkTarget(NamedTuple):
    host: str
    scheme: str | None
    port: int | None


def _parse_network_target(raw: str, *, allow_resource_path: bool) -> NetworkTarget:
    value = raw.strip()
    if not value:
        raise InvalidOriginError("network target must not be empty")
    if "://" not in value:
        unbracketed = value.strip("[]")
        try:
            address = ipaddress.ip_address(unbracketed)
        except ValueError:
            pass
        else:
            return NetworkTarget(host=str(address), scheme=None, port=None)
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if parsed.username or parsed.password:
        raise InvalidOriginError("network target must not contain credentials")
    if not allow_resource_path and (
        parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        raise InvalidOriginError(
            "network target accepts only a domain/IP with optional scheme and port"
        )
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise InvalidOriginError("network target scheme must be http or https")
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise InvalidOriginError("network target contains an invalid host or port") from exc
    if not host or not _valid_host(host):
        raise InvalidOriginError("network target contains an invalid domain or IP")
    return NetworkTarget(host=host, scheme=parsed.scheme or None, port=port)


def parse_network_target(raw: str) -> NetworkTarget:
    """Parse a Scope entry containing only a host and optional scheme/port."""

    return _parse_network_target(raw, allow_resource_path=False)


def parse_origin(raw: str) -> Origin:
    """Parse the legacy network-only Origin shape.

    New Tasks no longer call this function; Origin is free-form text.  It is
    retained for compatibility with callers that explicitly need to parse a
    network target and with historical tests/import paths.
    """

    target = parse_network_target(raw)
    host = target.host
    port = target.port
    canonical_host = f"[{host}]" if ":" in host else host
    authority = f"{canonical_host}:{port}" if port is not None else canonical_host
    original_input = f"{target.scheme}://{authority}" if target.scheme else authority
    return Origin(
        host=host,
        scheme=target.scheme,
        port=port,
        original_input=original_input,
    )


def normalize_scope_target(raw: str) -> str:
    value = raw.strip().lower().rstrip(".")
    if value.startswith("*."):
        suffix = parse_network_target(value[2:]).host
        if ":" in suffix:
            raise InvalidOriginError("wildcard scope targets require a domain")
        return f"*.{suffix}"
    return parse_network_target(value).host


def parse_intent_network_target(raw: str) -> NetworkTarget | None:
    """Return a network target, or ``None`` for an explicit logical target.

    Intent targets may describe non-network objects such as Mini Program
    routes or Artifact IDs.  Those labels do not grant network access.  Values
    that look network-like are still parsed strictly so a malformed or
    out-of-Scope URL cannot silently masquerade as a logical label.
    """

    value = raw.strip()
    lowered = value.lower()
    if not value:
        return None
    if lowered.startswith(_LOGICAL_TARGET_PREFIXES):
        return None
    if value.startswith(("/", "./", "../")) or any(char.isspace() for char in value):
        return None
    try:
        return _parse_network_target(value, allow_resource_path=True)
    except InvalidOriginError:
        if _looks_network_like(value):
            raise
        return None


def scope_entry_matches_host(entry: str, host: str) -> bool:
    """Return whether a canonical Task Scope entry contains ``host``.

    Scope entries deliberately store only hosts (or ``*.domain`` wildcards),
    while Reason proposals may use a bare authority or a full HTTP origin.  A
    single matcher keeps allow- and deny-list behaviour identical.
    """

    if entry.startswith("*."):
        return host.endswith(entry[1:])
    return host == entry or host.endswith(f".{entry}")


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host.split(".")
        return len(host) <= 253 and all(_DOMAIN_LABEL.fullmatch(label) for label in labels)


def _looks_network_like(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "//", "localhost")):
        return True
    if "." in value or value.startswith("["):
        return True
    host_candidate = value.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(host_candidate.strip("[]"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"[a-zA-Z0-9-]+:\d+", value))
