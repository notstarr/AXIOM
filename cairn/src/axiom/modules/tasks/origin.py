from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from axiom.modules.tasks.schemas import Origin


_DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def parse_origin(raw: str) -> Origin:
    value = (raw or "").strip()
    if not value or any(char.isspace() for char in value) or "*" in value:
        raise ValueError("origin must be one domain or IP")

    has_scheme = "://" in value
    parsed = urlsplit(value if has_scheme else f"//{value}")
    scheme = parsed.scheme.lower() if has_scheme else None
    if scheme is not None and scheme not in {"http", "https"}:
        raise ValueError("origin scheme must be http or https")
    if parsed.username or parsed.password:
        raise ValueError("origin must not include credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("origin must not include path, query, or fragment")

    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ValueError("origin host is required")
    _validate_host(host)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin port is invalid") from exc

    return Origin(host=host, scheme=scheme, port=port, original_input=value)


def _validate_host(host: str) -> None:
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("origin domain is invalid") from exc
    if len(ascii_host) > 253 or not all(_DOMAIN_LABEL.match(label) for label in ascii_host.split(".")):
        raise ValueError("origin domain is invalid")
