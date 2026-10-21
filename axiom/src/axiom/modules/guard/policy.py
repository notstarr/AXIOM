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
    return bool(_NETWORK_VERB.search(command))


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
                            "content": json.dumps(
                                {
                                    "operation": normalized,
                                    "task_id": request.task_id,
                                    "intent_id": request.intent_id,
                                    "reason_run_id": request.reason_run_id,
                                    "plan_run_id": request.plan_run_id,
                                    "review_run_id": request.review_run_id,
                                    "phase": request.phase,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": 64,
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
    verdict = _verdict(raw)
    if verdict == "1":
        return GuardDecision(decision="ALLOW", source="model", **diagnostic)
    if verdict == "0":
        return GuardDecision(
            decision="BLOCK", reason="监管模型判定阻断", source="model", **diagnostic
        )
    # The judge answers with a single digit.  Anything else (empty completion,
    # markup, prose) is treated as no answer and keeps the fail-open default.
    return GuardDecision(
        decision="ALLOW",
        reason="监管模型未返回 0/1，按 fail-open 放行",
        source="fail_open",
        **diagnostic,
    )


def _verdict(raw: str) -> str:
    """Return ``"0"`` or ``"1"`` when the completion carries one, else ``""``.

    Only the first non-empty line counts, so trailing prose cannot flip a
    verdict.  Markdown fences and emphasis characters are dropped first because
    chat models like to decorate a one-character answer.
    """

    for line in raw.splitlines():
        cleaned = line.strip().strip("`*_ \t\r\n'\"")
        if cleaned:
            return cleaned[0] if cleaned[0] in "01" else ""
    return ""

