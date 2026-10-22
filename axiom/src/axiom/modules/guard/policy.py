from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from axiom.modules.guard.defaults import SYSTEM_PROMPT
from axiom.modules.guard.schemas import GuardDecision, GuardRequest


def evaluate(request: GuardRequest, config, engagement_mode: str = "") -> GuardDecision:
    normalized = normalize(request.tool_name, request.input)
    if config.guard_mode == "off":
        return GuardDecision(decision="ALLOW", source="disabled")
    if _whitelisted(normalized, config):
        return GuardDecision(decision="ALLOW", reason="只读或本地侦察操作", source="whitelist")
    prompt, rules = _resolve_profile(config, engagement_mode)
    blocked = _hard_block(normalized, rules)
    if blocked:
        return GuardDecision(decision="BLOCK", reason=blocked, source="hard_rule")
    if not _external_operation(normalized):
        return GuardDecision(decision="ALLOW", reason="本地操作", source="whitelist")
    return _model_decision(request, normalized, config, prompt)


def _resolve_profile(config, engagement_mode: str) -> tuple[str, list]:
    """Return the prompt and rule set for this engagement mode.

    The allowlist is shared across modes; only the prompt and the hard rules
    differ.  A mode with no profile (or a profile that only overrides one of the
    two fields) falls back to the legacy global fields, so existing stored
    configurations keep their historical behaviour.
    """

    prompt = config.guard_system_prompt
    rules = list(config.guard_hard_block_rules)
    profiles = getattr(config, "guard_profiles", None) or {}
    profile = profiles.get(engagement_mode) if engagement_mode else None
    if profile is not None:
        if profile.system_prompt:
            prompt = profile.system_prompt
        if profile.hard_block_rules:
            rules = list(profile.hard_block_rules)
    return prompt, rules



def normalize(tool_name: str, payload: Any) -> dict[str, str]:
    data = payload if isinstance(payload, dict) else {"value": payload}
    name = tool_name.strip().lower()
    if name == "bash":
        command = str(data.get("command") or data.get("cmd") or "")
    elif name in {"read", "write", "edit", "ls", "grep", "find"}:
        command = str(data.get("path") or data.get("file_path") or "")
    else:
        command = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return {"tool_name": name, "command": command[:12_000]}


# The bsk wrapper drives the operator's own browser.  The operator asked for it
# to be waved through rather than judged: it is a tool the engagement uses for
# login state, not a target action, so neither the rule set nor the model has
# anything useful to say about it.
_BSK_INVOCATION = re.compile(
    r"(?:^|[;&]\s*|\|\s+)(?:sudo\s+)?"
    r"(?:\$\{?AXIOM_BSK\}?|\$\{?B\}?|axiom-bsk|/\S*axiom-bsk|bsk)(?=\s|$)",
)


def _is_bsk_command(command: str) -> bool:
    """True when the command runs the bsk wrapper.

    Covers the three shapes the workers actually use: ``$AXIOM_BSK ...``,
    ``/opt/axiom/src/axiom/dispatcher/tools/axiom-bsk ...`` and a shell alias
    assigned earlier in the same command (``B=.../axiom-bsk; $B ...``) - the
    assignment itself is caught by the path match.
    """

    return bool(_BSK_INVOCATION.search(command))


# The worker's own directories.  Every command it runs starts with a cd into one
# of these, so a naive judge reads the path and calls a target-side action "an
# attack on our own platform".  Measured on 1264 recorded decisions: 28 of 46
# model blocks were exactly that mistake.
_WORKSPACE_ROOT = re.compile(
    r"/var/lib/axiom/(?:sandboxes|contexts|tasks)\b"
)
# A write that stays inside those directories is housekeeping (evidence files,
# scratch creds.json).  A write anywhere else is the engagement.
_DESTRUCTIVE_WRITE = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?"
    r"(?:rm\s+-(?:[rRf]+\s+)+[^;&|]*"
    r"|mv\s+[^;&|]*"
    r"|truncate\s+[^;&|]*"
    r"|sed\s+-i[^;&|]*"
    r"|tee\s+[^;&|]*"
    r"|cp\s+[^;&|]*)",
)


def _only_touches_own_workspace(command: str) -> bool:
    """True when the command never writes outside the worker's own directories.

    The verdict is about where the destructive writes point, not where the
    command happens to cd.  A ``rm -f evidence/scratch.txt`` is housekeeping even
    though the path resolves under /var/lib/axiom; an ``rm`` that names a client
    path is not.
    """

    if not _WORKSPACE_ROOT.search(command):
        return False
    for match in _DESTRUCTIVE_WRITE.finditer(command):
        segment = match.group(0)
        if not _WORKSPACE_ROOT.search(segment):
            return False
    return True


def _whitelisted(normalized: dict[str, str], config) -> bool:
    whitelist_tools = {str(item).strip().casefold() for item in config.guard_whitelist_tools}
    whitelist_bash_verbs = {
        str(item).strip().casefold() for item in config.guard_whitelist_bash_verbs
    }
    if normalized["tool_name"] in whitelist_tools:
        return True
    if normalized["tool_name"] != "bash":
        return False
    command = normalized["command"].strip()
    if _is_bsk_command(command):
        return True
    # Pure own-workspace housekeeping never reaches the judge - there is nothing
    # to judge.  A command that also talks to the network keeps going, so the
    # target side still gets reviewed.
    if _only_touches_own_workspace(command) and not _NETWORK_VERB.search(command) \
            and not _URL.search(command):
        return True
    if not command or any(token in command for token in ("|", ";", ">", "<", "`", "$(", "\n")):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or PurePosixPath(tokens[0]).name.casefold() not in whitelist_bash_verbs:
        return False
    return not (tokens[0] == "find" and any(item in {"-delete", "-exec", "-execdir"} for item in tokens))


def _hard_block(normalized: dict[str, str], rules: list) -> str:
    text = normalized["command"]
    for rule in rules:
        if not rule.enabled:
            continue
        if re.search(rule.pattern, text, re.I):
            return rule.reason
    return ""


_NETWORK_VERB = re.compile(
    r"\b(curl|wget|httpx|nmap|subfinder|dirsearch|sqlmap|nuclei|ssh|ftp|nc|"
    r"socat|telnet|rsync|openssl\s+s_client)\b",
    re.I,
)
_URL = re.compile(r"\b(?:https?|wss?|ftp)://", re.I)
# An HTTP call written as a bare path or as ``GET <path>`` carries no scheme, so
# the URL check misses it: ``GET /cgi/api/ap/delete/02:00:00:00:09:01`` was filed
# as a local operation and never judged.
_HTTP_PATH = re.compile(
    r"(?:^|[;&|]\s*)(?:GET|POST|PUT|DELETE|PATCH|HEAD)\s+/[\w./-]"
    r"|\b(?:curl|wget)\b[^|;&]*\s/[\w.-]+/",
    re.I,
)
_OPAQUE_SCRIPT = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?"
    r"(?:python3?\s+[^\s|;&<>]+\.py|"
    r"(?:bash|sh|zsh)\s+[^\s|;&<>]+\.(?:sh|bash)|"
    r"(?:bash|sh|zsh)\s+-c\b)",
    re.I,
)
# Inline ``python3 -c "..."`` and heredocs keep their code inside the command
# string, so the guard can still read it; only a bare script *file* hides the
# body.  Splitting on that line keeps local housekeeping out of the judge while
# nothing opaque escapes review.


# Network reach without a URL or a known verb.  The worker uses ``python3 -c``
# for nearly all of its local housekeeping, so the whole form cannot be treated
# as opaque - but the same form can open a socket.  These two patterns separate
# a JSON-wrangling one-liner from one that talks to the network.
_NET_LIB = re.compile(
    r"\b(?:requests|urllib|urlopen|http\.client|httpx|aiohttp|socket|socketserver|"
    r"paramiko|ftplib|telnetlib|smtplib|pycurl|websocket|websockets|scapy|dnspython)\b",
    re.I,
)
_SHELL_NET = re.compile(
    r"/dev/(?:tcp|udp)/|"
    r"\b(?:IO::Socket|LWP::UserAgent|Net::SSLeay)\b|"
    r"\bexec\s+\d+<>|"
    r"\b(?:nc|ncat|socat)\s[^|;&]*-e\b",
    re.I,
)


def _external_operation(normalized: dict[str, str]) -> bool:
    name = normalized["tool_name"]
    if (
        name.startswith("mcp")
        or name.startswith("browser_")
        or name in {"browser", "browser-control"}
    ):
        return True
    if name != "bash":
        # Local VPS housekeeping (read/write/edit/ls/grep/find) never reaches the
        # judge: the worker already runs as the operator and the guard is not the
        # boundary for its own filesystem.
        return False
    command = normalized["command"]
    if _URL.search(command) or _OPAQUE_SCRIPT.search(command):
        return True
    if _NET_LIB.search(command) or _SHELL_NET.search(command):
        return True
    if _HTTP_PATH.search(command):
        return True
    return bool(_NETWORK_VERB.search(command))


def _judge_input(normalized: dict[str, str], request: GuardRequest) -> str:
    """Render one tool call the way the prompt's examples show it.

    ``bash  <command>`` for shell calls, ``<tool>  <path>`` otherwise.  The
    engagement phase is appended only when it is set, since it is context rather
    than part of the operation.
    """

    name = normalized.get("tool_name") or "tool"
    body = normalized.get("command") or ""
    line = f"{name}  {body}"
    phase = (request.phase or "").strip()
    if phase:
        line += f"\n[phase] {phase}"
    return line


def _model_decision(
    request: GuardRequest, normalized: dict[str, str], config, system_prompt: str
) -> GuardDecision:
    base_url = config.guard_model_base_url.strip().rstrip("/")
    model = config.guard_model_name.strip()
    key = config.guard_api_key.strip()
    if not base_url or not model or not key:
        return GuardDecision(
            decision="ALLOW",
            reason="监管模型未配置，按 fail-open 放行",
            source="fail_open",
        )
    started = time.perf_counter()
    try:
        proxy = (
            config.network_proxy_url.strip()
            if config.network_proxy_mode == "http" and config.network_proxy_url.strip()
            else None
        )
        with httpx.Client(timeout=config.guard_timeout_seconds, proxy=proxy) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": system_prompt.strip() or SYSTEM_PROMPT,
                        },
                        {
                            "role": "user",
                            # Plain text, matching the few-shot examples in the
                            # system prompt.  A JSON envelope made the same
                            # command read differently: measured on one SSRF
                            # command, JSON produced "会破坏我方平台自身数据"
                            # (BLOCK) while the bare command produced "尝试读取
                            # 云实例凭证" (ALLOW).  The wrapper keys carried no
                            # information the judge acts on, only framing it
                            # could mistake for structure.
                            "content": _judge_input(normalized, request),
                        },
                    ],
                    "temperature": 0,
                    # One character of output is all the contract needs.  A
                    # reasoning model left enabled spends its whole budget on
                    # think tokens first: measured 5.5-12.2s per call against a
                    # 3s timeout, and the reply that arrived was empty because
                    # max_tokens ran out before the digit.  Disabled, the same
                    # call answers in 0.5s with completion_tokens=1.
                    "max_tokens": 64,
                    **({"enable_thinking": False} if config.guard_thinking_enabled is False else {}),
                },
            )
        response.raise_for_status()
        raw = str(response.json()["choices"][0]["message"]["content"])
    except Exception as exc:
        return GuardDecision(
            decision="ALLOW",
            reason=f"监管模型不可用，按 fail-open 放行：{type(exc).__name__}",
            source="fail_open",
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    diagnostic = {"model_latency_ms": latency_ms, "raw_model_output": raw.strip()[:2_000]}
    verdict, detail = _parse_verdict(raw)
    if verdict == "1":
        return GuardDecision(decision="ALLOW", reason=detail, source="model", **diagnostic)
    if verdict == "0":
        # The worker sees this string: it is returned to Explore as the block
        # reason, so it has to be short enough to read in a tool result and
        # specific enough to act on.
        return GuardDecision(
            decision="BLOCK",
            reason=detail or "监管模型判定阻断",
            source="model",
            **diagnostic,
        )
    # The judge answers with a digit and an optional short reason.  Anything else
    # (empty completion, markup, prose) is treated as no answer and keeps the
    # fail-open default.
    return GuardDecision(
        decision="ALLOW",
        reason="监管模型未返回 0/1，按 fail-open 放行",
        source="fail_open",
        **diagnostic,
    )


REASON_MAX_CHARS = 40


def _parse_verdict(raw: str) -> tuple[str, str]:
    """Split a judge reply into its verdict and its short reason.

    The contract is ``0`` or ``1`` optionally followed by ``:`` and a reason:
    ``0:会锁死管理员账号`` blocks, ``1:只读探测`` allows.  Only the first
    non-empty line counts, so trailing prose cannot flip a verdict.

    Models drift from the contract even when told not to - a measured reply was
    ``判定:0:经 bsk 访问公网第三方接口``.  So the digit is located rather than
    assumed to lead: the first ``0``/``1`` that stands as a token is the verdict,
    and whatever follows it becomes the reason.  Anything else returns
    ``("", "")``, which the caller treats as no answer rather than inventing a
    verdict.

    Returns the reason capped at REASON_MAX_CHARS.
    """

    digit = re.compile(r"(?<![\d.])([01])(?![\d.])(.*)", re.S)
    for line in raw.splitlines():
        cleaned = line.strip().strip("`*_ \t\r\n'\"")
        if not cleaned:
            continue
        found = digit.search(cleaned)
        if not found:
            return "", ""
        verdict = found.group(1)
        rest = found.group(2)
        previous = None
        while rest != previous:
            previous = rest
            rest = rest.lstrip("`*_ \t:：,，-|")
        return verdict, rest.strip().strip("`*_ ").replace("\n", " ")[:REASON_MAX_CHARS]
    return "", ""

