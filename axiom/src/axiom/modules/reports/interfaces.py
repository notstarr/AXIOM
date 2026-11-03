"""What a task actually touched on the wire: hosts, interfaces, and which finding needed them.

The delivered report has a chapter the single-finding export never needed — the asset and
interface surface of the whole engagement.  The data is already there: every reproduction
step carries the real request it sent, and a real request carries ``Host:`` plus a request
line.  Reading those two things is exact; reading ``entry_point`` is not, because that
field is prose:

    "(SQLite UNION 注入) → POST /api/mall/auth/admin/login → GET /mall/admin"

So the index is built from ``exchanges`` only, and ``entry_point`` is never parsed.

Two decisions worth recording, because both change what the report claims:

**Path normalisation.** ``/api/mall/products/363/reviews`` and ``/api/mall/products/712/
reviews`` are one interface, not two.  Without folding, a 53-finding task reports 155
"interfaces" that are really 40.  Numeric segments become ``{id}``, UUIDs ``{uuid}``, and
the raw form is kept on the row so a reader can still see a concrete example.

**Unattributed packets.** A request with no parsable ``Host:`` is not dropped.  Dropping it
would make the chapter silently understate what was touched; it goes into a separate bucket
that the renderer prints with its own heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    # Type-only: the report package is imported by these modules at runtime.
    from axiom.modules.vulnerabilities.schemas import Vulnerability


# ``GET /api/x?q=1 HTTP/1.1`` — absolute-form (proxy style) is accepted too, because a
# worker pasting a request it captured through a proxy writes that shape.
_REQUEST_LINE = re.compile(r"^([A-Z][A-Z0-9_-]*)\s+(\S+)\s+HTTP/\d(?:\.\d)?\s*$")
_STATUS_LINE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})")
_HOST_LINE = re.compile(r"^Host:\s*(\S+)\s*$", re.IGNORECASE)
# Path segments we fold so one interface is one row.
_NUMERIC_SEGMENT = re.compile(r"^\d+$")
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEXISH_SEGMENT = re.compile(r"^(?=.*\d)[0-9a-f]{16,}$", re.IGNORECASE)

UNATTRIBUTED_HOST = ""

# Worst-first ordering, used to pick the ``最高等级`` cell per host.  Kept here rather than
# required from callers: a caller that forgets to pass it would silently report no severity
# at all, which reads as "this host is fine".
SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


@dataclass(frozen=True, slots=True)
class Exchange:
    """One request/response pair reduced to what the index needs."""

    method: str
    path: str
    host: str
    status: int | None
    raw_path: str


@dataclass
class InterfaceRow:
    """One ``(host, method, normalised path)`` and who used it."""

    host: str
    method: str
    path: str
    calls: int = 0
    statuses: set[int] = field(default_factory=set)
    finding_ids: set[str] = field(default_factory=set)
    # A concrete path, so the reader can replay something real instead of a template.
    example_path: str = ""

    @property
    def status_text(self) -> str:
        if not self.statuses:
            return ""
        return "/".join(str(code) for code in sorted(self.statuses))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.host, self.method, self.path)


@dataclass
class HostRow:
    """One host and what reached it."""

    host: str
    calls: int = 0
    finding_ids: set[str] = field(default_factory=set)
    interfaces: set[tuple[str, str]] = field(default_factory=set)
    worst_severity: str = ""
    first_finding_id: str = ""


@dataclass
class InterfaceIndex:
    hosts: list[HostRow]
    interfaces: list[InterfaceRow]
    unattributed: list[InterfaceRow]

    @property
    def host_count(self) -> int:
        return len(self.hosts)

    @property
    def interface_count(self) -> int:
        return len(self.interfaces) + len(self.unattributed)

    @property
    def call_count(self) -> int:
        return sum(row.calls for row in self.hosts) + sum(
            row.calls for row in self.unattributed
        )

    def interfaces_for(self, finding_id: str) -> list[InterfaceRow]:
        """The interfaces one finding touched, in stable order."""
        return [
            row
            for row in (*self.interfaces, *self.unattributed)
            if finding_id in row.finding_ids
        ]


def parse_exchange(request: str, response: str = "") -> Exchange | None:
    """Reduce one packet pair, or ``None`` when the request has no request line.

    ``None`` is not an error the caller should hide: a malformed packet means the index
    cannot claim to know where that request went, and the report says so rather than
    counting it as some other host.
    """
    lines = (request or "").splitlines()
    if not lines:
        return None
    match = _REQUEST_LINE.match(lines[0].strip())
    if match is None:
        return None
    method, target = match.group(1).upper(), match.group(2)
    path = _path_of(target)
    host = ""
    for line in lines[1:]:
        host_match = _HOST_LINE.match(line.strip())
        if host_match is not None:
            host = _normalise_host(host_match.group(1))
            break
    if not host:
        # Absolute-form target carries the authority itself.
        host = _normalise_host(_authority_of(target))
    status = None
    for line in (response or "").splitlines()[:1]:
        status_match = _STATUS_LINE.match(line.strip())
        if status_match is not None:
            status = int(status_match.group(1))
    return Exchange(
        method=method,
        path=normalise_path(path),
        host=host,
        status=status,
        raw_path=path,
    )


def normalise_path(path: str) -> str:
    """Fold identifier-shaped segments so one interface is one row.

    Query strings are dropped: ``?q=`` and ``?id=1`` are the same interface with different
    input, and the raw form is kept on the example so nothing is lost.
    """
    clean = (path or "").split("?", 1)[0].split("#", 1)[0]
    if not clean:
        return "/"
    segments = []
    for segment in clean.split("/"):
        if _NUMERIC_SEGMENT.match(segment):
            segments.append("{id}")
        elif _UUID_SEGMENT.match(segment):
            segments.append("{uuid}")
        elif _HEXISH_SEGMENT.match(segment):
            segments.append("{hash}")
        else:
            segments.append(segment)
    folded = "/".join(segments)
    return folded if folded.startswith("/") else f"/{folded}"


def _path_of(target: str) -> str:
    """The path part of an origin-form or absolute-form request target."""
    if "://" in target:
        _, _, rest = target.partition("://")
        slash = rest.find("/")
        return rest[slash:] if slash >= 0 else "/"
    return target


def _authority_of(target: str) -> str:
    if "://" not in target:
        return ""
    _, _, rest = target.partition("://")
    authority = rest.split("/", 1)[0]
    # Drop userinfo: `user:pass@host` never belongs in a delivered report.
    return authority.rsplit("@", 1)[-1]


def _normalise_host(host: str) -> str:
    """Lower-case, strip the port, strip a trailing dot."""
    clean = (host or "").strip().lower().rstrip(".")
    if not clean:
        return ""
    if clean.startswith("["):  # IPv6 literal, keep the brackets
        closing = clean.find("]")
        return clean[: closing + 1] if closing > 0 else clean
    return clean.split(":", 1)[0]


def build_index(
    vulnerabilities: Iterable["Vulnerability"],
    *,
    severity_rank: dict[str, int] | None = None,
) -> InterfaceIndex:
    """Aggregate every reproduction exchange in the task into hosts and interfaces."""
    rank = severity_rank if severity_rank is not None else SEVERITY_RANK
    hosts: dict[str, HostRow] = {}
    interfaces: dict[tuple[str, str, str], InterfaceRow] = {}
    unattributed: dict[tuple[str, str, str], InterfaceRow] = {}

    for vulnerability in vulnerabilities:
        reproduction = getattr(vulnerability, "reproduction", None)
        if reproduction is None:
            continue
        severity = str(getattr(vulnerability, "severity", "") or "")
        for step in reproduction.steps:
            for exchange in step.exchanges:
                parsed = parse_exchange(exchange.request, exchange.response)
                if parsed is None:
                    continue
                row_key = (parsed.host, parsed.method, parsed.path)
                bucket = interfaces if parsed.host else unattributed
                row = bucket.get(row_key)
                if row is None:
                    row = InterfaceRow(
                        host=parsed.host,
                        method=parsed.method,
                        path=parsed.path,
                        example_path=parsed.raw_path,
                    )
                    bucket[row_key] = row
                row.calls += 1
                if parsed.status is not None:
                    row.statuses.add(parsed.status)
                row.finding_ids.add(vulnerability.id)
                if not parsed.host:
                    continue

                host_row = hosts.get(parsed.host)
                if host_row is None:
                    host_row = HostRow(host=parsed.host, first_finding_id=vulnerability.id)
                    hosts[parsed.host] = host_row
                host_row.calls += 1
                host_row.finding_ids.add(vulnerability.id)
                host_row.interfaces.add((parsed.method, parsed.path))
                if severity and rank.get(severity, 0) > rank.get(host_row.worst_severity, 0):
                    host_row.worst_severity = severity

    return InterfaceIndex(
        hosts=sorted(hosts.values(), key=lambda row: (-row.calls, row.host)),
        interfaces=sorted(
            interfaces.values(),
            key=lambda row: (row.host, -row.calls, row.method, row.path),
        ),
        unattributed=sorted(
            unattributed.values(), key=lambda row: (-row.calls, row.method, row.path)
        ),
    )


__all__ = [
    "Exchange",
    "HostRow",
    "InterfaceIndex",
    "InterfaceRow",
    "SEVERITY_RANK",
    "UNATTRIBUTED_HOST",
    "build_index",
    "normalise_path",
    "parse_exchange",
]
