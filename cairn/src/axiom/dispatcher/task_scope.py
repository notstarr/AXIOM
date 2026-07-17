from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import ipaddress
import json
import logging
from pathlib import PurePosixPath
import re
import sqlite3
from typing import Any

from axiom.server.db import current_db_path


LOG = logging.getLogger(__name__)

NETWORK_TARGET_TOOLS = frozenset(
    {
        "curl",
        "dig",
        "dnsx",
        "wget",
        "gau",
        "host",
        "katana",
        "mongo",
        "mongosh",
        "mysql",
        "naabu",
        "nc",
        "netcat",
        "nmap",
        "masscan",
        "httpx",
        "nslookup",
        "openssl",
        "ping",
        "ping6",
        "psql",
        "redis-cli",
        "scp",
        "sftp",
        "socat",
        "sqlmap",
        "ssh",
        "subfinder",
        "telnet",
        "traceroute",
        "waybackurls",
        "dirsearch",
        "ffuf",
        "feroxbuster",
        "nikto",
    }
)

_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+")
_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})\.)+[A-Za-z]{2,63}(?![A-Za-z0-9_.-])"
)
_IP_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9_.-])")
_DEV_SOCKET_RE = re.compile(r"/dev/(?:tcp|udp)/(?P<host>[^/\s]+)/\d+", re.IGNORECASE)
_NETWORK_VERB_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:" + "|".join(sorted(NETWORK_TARGET_TOOLS)) + r")(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_HEREDOC_RE = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<marker>[A-Za-z0-9_./-]+)(?P=quote)")
_IGNORED_FILE_SUFFIXES = frozenset(
    {
        "css", "csv", "go", "html", "ini", "java", "js", "json", "log", "md", "py",
        "sh", "sql", "svg", "toml", "ts", "txt", "yaml", "yml",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeTaskScope:
    enabled: bool
    targets: frozenset[str]
    exclude: frozenset[str]

    def allows(self, host: str) -> bool:
        normalized = _normalize_host(host)
        if not self.enabled or not normalized:
            return True
        if any(_matches(normalized, rule) for rule in self.exclude):
            return False
        return not self.targets or any(_matches(normalized, rule) for rule in self.targets)


def load_task_scope(project_id: str | None) -> RuntimeTaskScope:
    if not project_id:
        return RuntimeTaskScope(False, frozenset(), frozenset())
    db_path = current_db_path()
    if not db_path.exists():
        return RuntimeTaskScope(False, frozenset(), frozenset())
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT task_scope_enabled, task_scope_json FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("failed to read task scope project=%s error=%s", project_id, exc)
        return RuntimeTaskScope(False, frozenset(), frozenset())
    finally:
        conn.close()
    if row is None or not bool(row["task_scope_enabled"]):
        return RuntimeTaskScope(False, frozenset(), frozenset())
    try:
        payload = json.loads(row["task_scope_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return RuntimeTaskScope(
        True,
        frozenset(_normalize_rules(payload.get("targets"))),
        frozenset(_normalize_rules(payload.get("exclude"))),
    )


def _normalize_rules(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in (_normalize_host(str(raw)) for raw in value) if item}


def _normalize_host(value: str) -> str:
    text = value.strip().lower().strip(".")
    if "://" in text:
        from urllib.parse import urlparse

        text = (urlparse(text).hostname or "").lower().strip(".")
    return text.split("/", 1)[0].split(":", 1)[0].strip(".")


def _matches(host: str, rule: str) -> bool:
    if not rule:
        return False
    if any(char in rule for char in "*?["):
        return fnmatch.fnmatch(host, rule)
    return host == rule or host.endswith(f".{rule}")


def out_of_scope_host_for_argv(scope: RuntimeTaskScope, argv: list[str]) -> str | None:
    """Return the first forbidden host in a direct network-tool invocation."""
    if not scope.enabled or not argv:
        return None
    tool = PurePosixPath(str(argv[0])).name.lower()
    if tool not in NETWORK_TARGET_TOOLS:
        return None
    for token in argv:
        for host in extract_hosts_from_text(str(token), allow_bare=True):
            if not scope.allows(host):
                return host
    return None


def out_of_scope_host_for_tool(
    scope: RuntimeTaskScope,
    tool_name: str,
    input_payload: Any,
) -> str | None:
    """Return the first forbidden host in an in-agent tool call.

    PI normally exposes network access through ``bash``.  Explicit URLs/IPs
    are always inspected; bare DNS names are inspected only when the command
    contains a known network utility, which avoids treating report filenames
    such as ``models.json`` as domains.
    """
    if not scope.enabled:
        return None
    tool = str(tool_name or "").strip().lower()
    text = _tool_input_text(tool, input_payload)
    if not text:
        return None
    text = _strip_shell_heredoc_bodies(text) if tool == "bash" else text
    allow_bare = tool in NETWORK_TARGET_TOOLS or (
        tool == "bash" and bool(_NETWORK_VERB_RE.search(text))
    )
    for host in extract_hosts_from_text(text, allow_bare=allow_bare):
        if not scope.allows(host):
            return host
    return None


def extract_hosts_from_text(text: str, *, allow_bare: bool) -> list[str]:
    from urllib.parse import urlparse

    value = str(text or "")
    hosts: list[str] = []
    seen: set[str] = set()

    def add(host: str | None) -> None:
        normalized = _normalize_host(host or "")
        if normalized and normalized not in seen:
            seen.add(normalized)
            hosts.append(normalized)

    for match in _URL_RE.finditer(value):
        try:
            add(urlparse(match.group(0).rstrip(".,;)]}")).hostname)
        except ValueError:
            continue
    for match in _IP_RE.finditer(value):
        try:
            ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        add(match.group(0))
    for match in _DEV_SOCKET_RE.finditer(value):
        add(match.group("host"))
    if allow_bare:
        for match in _HOST_RE.finditer(value):
            candidate = match.group(0).lower().strip(".")
            if candidate.rsplit(".", 1)[-1] in _IGNORED_FILE_SUFFIXES:
                continue
            add(candidate)
    return hosts


def _tool_input_text(tool: str, payload: Any) -> str:
    if isinstance(payload, dict):
        preferred = []
        for key, value in payload.items():
            key_text = str(key).lower()
            if key_text in {"command", "cmd", "url", "uri", "host", "hostname", "target", "targets", "endpoint", "base_url"}:
                preferred.append(str(value))
        if preferred:
            return "\n".join(preferred)
        if tool == "bash":
            return str(payload.get("command") or payload.get("cmd") or "")
        return ""
    return str(payload or "")


def _strip_shell_heredoc_bodies(command: str) -> str:
    if "<<" not in command:
        return command
    kept: list[str] = []
    pending: list[str] = []
    for line in command.splitlines():
        if pending:
            if line.lstrip("\t").strip() == pending[0]:
                pending.pop(0)
                kept.append(line)
            continue
        kept.append(line)
        for match in _HEREDOC_RE.finditer(line):
            marker = match.group("marker").strip()
            if marker:
                pending.append(marker)
    return "\n".join(kept)
