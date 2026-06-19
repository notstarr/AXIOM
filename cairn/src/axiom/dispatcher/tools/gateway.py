from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from urllib.parse import urlparse

from axiom.dispatcher.domain_blacklist import load_domain_blacklist


BUILTIN_TOOLS = frozenset(
    {
        "cat",
        "ls",
        "pwd",
        "echo",
        "printf",
        "sed",
        "awk",
        "cut",
        "sort",
        "uniq",
        "tr",
        "wc",
        "head",
        "tail",
        "grep",
        "rg",
        "curl",
        "wget",
        "jq",
        "python",
        "python3",
        "bash",
        "sh",
        "zsh",
        "nmap",
        "masscan",
        "httpx",
    }
)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    phase: str
    tool: str
    kind: str
    argv: list[str]


class ToolGateway:
    """
    Unified tool invocation gateway.

    Current MVP behavior:
    - Classify command as builtin/external
    - Optional blocklist enforcement
    - Return normalized invocation metadata
    """

    def __init__(self, *, blocked_tools: set[str] | None = None):
        self._blocked_tools = {name.strip().lower() for name in (blocked_tools or set()) if name.strip()}

    def prepare(self, phase: str, argv: list[str], *, project_id: str | None = None) -> ToolInvocation:
        tool = self._tool_name(argv)
        normalized = tool.lower()
        if normalized in self._blocked_tools:
            raise ValueError(f"tool blocked by policy: {tool}")
        if phase.startswith("bootstrap") or phase.startswith("explore"):
            try:
                blocked_domains = load_domain_blacklist(project_id)
            except TypeError:
                blocked_domains = load_domain_blacklist()
            blocked = self._match_blocked_domain(argv, blocked_domains)
            if blocked is not None:
                raise ValueError(f"blocked domain by policy: {blocked}")
        kind = "builtin" if normalized in BUILTIN_TOOLS else "external"
        return ToolInvocation(phase=phase, tool=tool, kind=kind, argv=list(argv))

    @staticmethod
    def _tool_name(argv: list[str]) -> str:
        if not argv:
            return "unknown"
        return PurePosixPath(argv[0]).name or "unknown"

    @staticmethod
    def _match_blocked_domain(argv: list[str], blocked_domains: set[str]) -> str | None:
        if not blocked_domains:
            return None
        for token in argv:
            for host in ToolGateway._extract_hosts(token):
                if ToolGateway._is_host_blocked(host, blocked_domains):
                    return host
        return None

    @staticmethod
    def _extract_hosts(token: str) -> list[str]:
        text = token.strip()
        if not text:
            return []
        hosts: list[str] = []
        if "://" in text:
            parsed = urlparse(text)
            if parsed.hostname:
                hosts.append(parsed.hostname.lower().strip("."))
        for candidate in re.findall(r"\b([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", text):
            hosts.append(candidate.lower().strip("."))
        return hosts

    @staticmethod
    def _is_host_blocked(host: str, blocked_domains: set[str]) -> bool:
        for blocked in blocked_domains:
            if host == blocked or host.endswith(f".{blocked}"):
                return True
        return False


_DEFAULT_GATEWAY = ToolGateway()


def default_gateway() -> ToolGateway:
    return _DEFAULT_GATEWAY
