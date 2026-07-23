from __future__ import annotations

import re


_HEADER_CREDENTIAL = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie)\s*:\s*).*$"
)
_KEY_VALUE_CREDENTIAL = re.compile(
    r"(?i)([\"']?(?:"
    r"authorization|proxy[-_ ]?authorization|"
    r"x[-_ ]?api[-_ ]?key|api[-_ ]?key|amap[-_ ]?key|app[-_ ]?key|"
    r"apm[-_ ]?(?:key|token)|magic[-_ ]?apm|"
    r"access[-_ ]?key[-_ ]?(?:id|secret)|secret[-_ ]?key|"
    r"client[-_ ]?secret|sts[-_ ]?token|security[-_ ]?token|"
    r"access[-_ ]?token|refresh[-_ ]?token|id[-_ ]?token|session[-_ ]?token|"
    r"organization[-_ ]?code|org[-_ ]?code|"
    r"token|cookie|set[-_ ]?cookie|password|passwd"
    r")[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED(?:_JWT)?\])"
    r"(?:bearer\s+)?([^\s\"',}\]]+)"
)
_URL_CREDENTIAL = re.compile(
    r"(?i)([?&](?:access[-_ ]?token|refresh[-_ ]?token|id[-_ ]?token|"
    r"api[-_ ]?key|apikey|token|signature|sig|password|passwd)="
    r")(?!\[REDACTED(?:_JWT)?\])([^&#\s\"']+)"
)
_BASIC_AUTH_URL = re.compile(r"(?i)(https?://[^\s/@:]+:)([^\s/@]+)(@)")
_BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_JWT_CREDENTIAL = re.compile(
    r"eyJ[A-Za-z0-9_=-]{20,}\.[A-Za-z0-9_=-]{20,}(?:\.[A-Za-z0-9_=-]{8,})?"
)


def redact_sensitive_text(value: str) -> str:
    """Remove credential material before evidence becomes durable context."""

    value = _HEADER_CREDENTIAL.sub(r"\1[REDACTED]", value)
    value = _KEY_VALUE_CREDENTIAL.sub(r"\1[REDACTED]", value)
    value = _URL_CREDENTIAL.sub(r"\1[REDACTED]", value)
    value = _BASIC_AUTH_URL.sub(r"\1[REDACTED]\3", value)
    value = _BEARER_CREDENTIAL.sub("Bearer [REDACTED]", value)
    return _JWT_CREDENTIAL.sub("[REDACTED_JWT]", value)
