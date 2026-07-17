from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from axiom.dispatcher.task_scope import (
    extract_hosts_from_text,
    load_task_scope,
    out_of_scope_host_for_argv,
)


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
            scope = load_task_scope(project_id)
            rejected = self._match_out_of_scope_domain(argv, scope)
            if rejected is not None:
                raise ValueError(f"domain outside task scope: {rejected}")
        kind = "builtin" if normalized in BUILTIN_TOOLS else "external"
        return ToolInvocation(phase=phase, tool=tool, kind=kind, argv=list(argv))

    @staticmethod
    def _tool_name(argv: list[str]) -> str:
        if not argv:
            return "unknown"
        return PurePosixPath(argv[0]).name or "unknown"

    @staticmethod
    def _match_out_of_scope_domain(argv: list[str], scope) -> str | None:
        return out_of_scope_host_for_argv(scope, argv)

    @staticmethod
    def _extract_hosts(token: str) -> list[str]:
        return extract_hosts_from_text(token, allow_bare=True)

_DEFAULT_GATEWAY = ToolGateway()


def default_gateway() -> ToolGateway:
    return _DEFAULT_GATEWAY
