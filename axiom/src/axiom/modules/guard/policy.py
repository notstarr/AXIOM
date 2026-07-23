from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from axiom.modules.guard.schemas import GuardDecision, GuardRequest


WHITELIST_TOOLS = ["read", "ls", "grep", "find"]
WHITELIST_BASH_VERBS = [
    "pwd",
    "whoami",
    "id",
    "uname",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "ifconfig",
    "ip",
    "env",
    "printenv",
]
HARD_BLOCK_RULES = [
    {"reason": "修改或重置用户密码", "pattern": r"\b(passwd|chpasswd|reset[_-]?password)\b"},
    {"reason": "删除或清空关键数据", "pattern": r"\b(drop\s+table|truncate\s+table|redis-cli\s+flush(all|db))\b|rm\s+-rf\s+(/|/etc|/var|/usr|/opt|/home|\*)"},
    {"reason": "停止或重启业务服务", "pattern": r"\b(systemctl\s+(stop|restart)|shutdown|reboot|service\s+\S+\s+(stop|restart))\b"},
    {"reason": "篡改账户或权限体系", "pattern": r"\b(useradd|userdel|usermod|groupadd|groupdel|grant\s+all|revoke\s+all)\b"},
    {"reason": "高并发或 DoS 操作", "pattern": r"\b(slowloris|hping3|ab\s+-n\s+\d{5,}|wrk\s+.*-c\s+\d{3,})\b"},
]
SYSTEM_PROMPT = """你是授权渗透测试的安全守门员。默认放行正常的漏洞探测与最小化验证；只有操作会直接破坏业务可用性、不可逆修改真实数据、触达真实用户权益、造成高并发拒绝服务或明显越过授权边界时才阻断。只回复 ALLOW 或 BLOCK:<不超过30字的原因>。"""


def evaluate(request: GuardRequest, config) -> GuardDecision:
    normalized = normalize(request.tool_name, request.input)
    if config.guard_mode == "off":
        return GuardDecision(decision="ALLOW", source="disabled")
    if _whitelisted(normalized):
        return GuardDecision(decision="ALLOW", reason="只读或本地侦察操作", source="whitelist")
    blocked = _hard_block(normalized)
    if blocked:
        return GuardDecision(decision="BLOCK", reason=blocked, source="hard_rule")
    if not _external_operation(normalized):
        return GuardDecision(decision="ALLOW", reason="本地操作", source="whitelist")
    return _model_decision(request, normalized, config)


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


def _whitelisted(normalized: dict[str, str]) -> bool:
    if normalized["tool_name"] in WHITELIST_TOOLS:
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
    if not tokens or PurePosixPath(tokens[0]).name.lower() not in WHITELIST_BASH_VERBS:
        return False
    return not (tokens[0] == "find" and any(item in {"-delete", "-exec", "-execdir"} for item in tokens))


def _hard_block(normalized: dict[str, str]) -> str:
    text = normalized["command"]
    for rule in HARD_BLOCK_RULES:
        if re.search(rule["pattern"], text, re.I):
            return rule["reason"]
    return ""


def _external_operation(normalized: dict[str, str]) -> bool:
    name = normalized["tool_name"]
    if name.startswith("mcp") or name in {"browser", "browser-control"}:
        return True
    return name == "bash" and bool(
        re.search(
            r"\b(curl|wget|httpx|nmap|subfinder|dirsearch|sqlmap|nuclei|ssh|ftp|nc|openssl\s+s_client)\b|https?://",
            normalized["command"],
            re.I,
        )
    )


def _model_decision(request: GuardRequest, normalized: dict[str, str], config) -> GuardDecision:
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
        with httpx.Client(timeout=config.guard_timeout_seconds) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "operation": normalized,
                                    "task_id": request.task_id,
                                    "intent_id": request.intent_id,
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
        raw = str(response.json()["choices"][0]["message"]["content"]).strip().splitlines()[0]
    except Exception as exc:
        return GuardDecision(
            decision="ALLOW",
            reason=f"监管模型不可用，按 fail-open 放行：{type(exc).__name__}",
            source="fail_open",
        )
    _ = started  # retained for debugger/profiler inspection without persisting secrets
    if raw == "ALLOW":
        return GuardDecision(decision="ALLOW", source="model")
    if raw.startswith("BLOCK:"):
        return GuardDecision(decision="BLOCK", reason=raw.partition(":")[2].strip()[:30], source="model")
    return GuardDecision(decision="ALLOW", reason="监管模型返回格式无效", source="fail_open")
