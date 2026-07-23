from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from axiom.modules.tasks.schemas import Origin
from axiom.shared.errors import InvalidOriginError


_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def parse_origin(raw: str) -> Origin:
    value = raw.strip()
    if not value:
        raise InvalidOriginError("origin must not be empty")
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if parsed.username or parsed.password:
        raise InvalidOriginError("origin must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise InvalidOriginError(
            "origin accepts only a domain/IP with optional scheme and port"
        )
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise InvalidOriginError("origin scheme must be http or https")
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise InvalidOriginError("origin contains an invalid host or port") from exc
    if not host or not _valid_host(host):
        raise InvalidOriginError("origin contains an invalid domain or IP")
    canonical_host = f"[{host}]" if ":" in host else host
    authority = f"{canonical_host}:{port}" if port is not None else canonical_host
    original_input = f"{parsed.scheme}://{authority}" if parsed.scheme else authority
    return Origin(
        host=host,
        scheme=parsed.scheme or None,
        port=port,
        original_input=original_input,
    )


def normalize_scope_target(raw: str) -> str:
    value = raw.strip().lower().rstrip(".")
    if value.startswith("*."):
        suffix = parse_origin(value[2:]).host
        if ":" in suffix:
            raise InvalidOriginError("wildcard scope targets require a domain")
        return f"*.{suffix}"
    return parse_origin(value).host


def scope_entry_matches_host(entry: str, host: str) -> bool:
    """Return whether a canonical Task Scope entry contains ``host``.

    Scope entries deliberately store only hosts (or ``*.domain`` wildcards),
    while Reason proposals may use a bare authority or a full HTTP origin.  A
    single matcher keeps allow- and deny-list behaviour identical.
    """

    return entry == host or (entry.startswith("*.") and host.endswith(entry[1:]))


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host.split(".")
        return len(host) <= 253 and all(_DOMAIN_LABEL.fullmatch(label) for label in labels)
