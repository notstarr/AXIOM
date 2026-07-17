from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

import requests

Decision = Literal["ALLOW", "BLOCK"]
DecisionSource = Literal["whitelist", "hard_rule", "model", "fail_open", "disabled"]

DEFAULT_WHITELIST_TOOLS = ["read", "ls", "grep", "find"]
DEFAULT_WHITELIST_BASH_VERBS = [
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

MODEL_SYSTEM_PROMPT = """你是授权渗透测试的安全守门员。绝大多数操作（包括各种攻击 payload）应放行；只拦截会直接破坏目标业务系统正常运行、造成真实用户数据丢失/不可逆篡改、触达真实用户权益、或明显越过授权边界的操作。

【核心原则：最小化验证】
- 单账号、单设备、单会话、单次请求链路证明即可。
- 默认倾向 ALLOW；拿不准时 ALLOW。
- “写”本身不是拦截理由，授权渗透测试会用到注入、上传、爆破、利用 payload 等动作；关键看是否破坏业务可用性、关键数据完整性、真实用户权益或授权边界。
- 下方 MUST BLOCK 是明确禁止边界；下方 MUST ALLOW 是在最小化验证前提下的正常测试动作。

【必须 BLOCK —— 明确命中才拦截】
1. 修改/重置任意用户或管理员密码、强制下线真实用户。
2. 修改服务器或服务配置：系统配置文件、Web/数据库服务参数、防火墙规则、启动项、计划任务。
3. 修改/创建/删除用户、角色、权限，或篡改账户体系。
4. 删除、清空或大规模破坏数据/文件：DROP TABLE、TRUNCATE、DELETE 全表、rm 重要文件、清空数据库。
5. 停止、禁用、重启业务服务：systemctl stop/restart、kill 关键进程、shutdown、reboot。
6. 压测、刷接口、高并发、大规模扫描导致 DoS/DDoS 或用户可感知影响。
7. 触达非本人真实用户数据，拖库，黑灰产行为，公开传播 PoC。
8. 内网渗透、横向移动、接触非测试靶机。
9. 保存/传播敏感数据（源码、用户资料等）；不知情下载敏感数据后应及时删除，对外披露必须有 SRC 授权。
10. 注入测试中读取表内真实数据，或使用自动化工具执行 UPDATE/DELETE/INSERT。
11. 越权测试中读取超过 5 组、批量操作，或对非本人测试账号做增删改。
12. 账号测试使用超过自己的 2 个账号；在不可注册系统获取账密且未先获管理员同意。
13. XSS 使用 alert/prompt/confirm 弹窗；盲打外带非 domain 信息；写入后不清理测试数据。
14. 命令执行中下载/读取源码或敏感文件，删除文件，或写入破坏性内容。
15. 扫号超过 50 个；对他人真实手机号做用户可感知测试。
16. 蠕虫测试；使用有社交关系的账号扩散或验证蠕虫能力。
17. 对网站后台页面使用扫描器；任何 DoS/DDoS 测试。

【必须 ALLOW —— 授权渗透的正常攻击手段】
- 各类漏洞探测与利用 payload：SQL 注入（UNION/布尔/时间盲注等）、XSS、命令注入、SSTI、反序列化、SSRF、路径遍历、文件包含、XXE，前提是只做最小化验证。
- 注入测试只证明可读、不读取表内真实数据、不自动化 UPDATE/DELETE/INSERT。
- 越权测试读取不超过 5 组；增删改仅限本人测试账号；不做批量操作。
- 账号测试仅使用自己的 2 个账号；不可注册系统获取账密前已获管理员同意。
- XSS 使用 console.log 验证；盲打仅外带 domain；包含测试后删除测试数据的清理动作。
- 命令执行只上传纯文本文件证明；不下载/读取源码或敏感文件；不删除、不写入破坏性内容。
- 扫号不超过 50 个，且不对他人真实手机号造成用户可感知影响。
- 蠕虫相关能力仅用隔离小号验证功能，不使用有社交关系账号，不做真实蠕虫传播。
- 文件上传测试（包含用于验证漏洞的 webshell 上传），前提是不破坏业务和真实数据。
- 常规速率的口令爆破（非 DoS 级、非真实用户可感知）。
- 信息收集、端口/目录扫描、枚举、抓取，前提是不触达后台高风险页面且不造成 DoS。
- 读取、查询、探测类操作，前提是不读取/保存真实敏感数据。
- 攻陷后在目标上执行的无害侦察命令：whoami/id/uname/ls/cat/ifconfig/ip/env 等。

只回复一行，不要任何额外内容：
- 放行：ALLOW
- 拦截：BLOCK:<不超过30字，说明命中的禁止项>"""


@dataclass(slots=True)
class GuardPolicyConfig:
    mode: Literal["off", "observe", "guarded", "enforce"] = "enforce"
    base_url: str = "https://api.siliconflow.cn/v1"
    model_name: str = "Qwen/Qwen3.5-9B"
    timeout_seconds: int = 3
    whitelist_tools: list[str] = field(default_factory=lambda: list(DEFAULT_WHITELIST_TOOLS))
    whitelist_bash_verbs: list[str] = field(default_factory=lambda: list(DEFAULT_WHITELIST_BASH_VERBS))
    system_prompt: str = MODEL_SYSTEM_PROMPT
    hard_block_rules: list[dict[str, object]] = field(default_factory=lambda: [dict(rule) for rule in DEFAULT_HARD_BLOCK_RULES])


@dataclass(slots=True)
class GuardEvaluation:
    decision: Decision
    reason: str
    source: DecisionSource
    normalized: dict[str, Any]
    model_name: str = ""
    model_latency_ms: int = 0
    raw_model_output: str = ""


def normalize_tool_input(tool_name: str, input_payload: Any) -> dict[str, Any]:
    data = input_payload if isinstance(input_payload, dict) else {"value": input_payload}
    command = ""
    path = ""
    if tool_name == "bash":
        command = str(data.get("command") or data.get("cmd") or "")
    elif tool_name in {"write", "edit", "read", "ls", "grep", "find"}:
        path = str(data.get("path") or data.get("file_path") or data.get("filePath") or "")
        command = path
    else:
        try:
            command = json.dumps(data, ensure_ascii=False, sort_keys=True)
        except TypeError:
            command = str(data)
    return {
        "tool_name": tool_name,
        "command": command[:12000],
        "path": path[:2000],
    }


DEFAULT_HARD_BLOCK_RULES: list[dict[str, object]] = [
    {"reason": "修改/重置用户密码", "pattern": r"\b(passwd|chpasswd)\b|\b(reset[_-]?password|change[_-]?password|set[-_]?new[-_]?secret)\b", "enabled": True},
    {"reason": "强制下线真实用户", "pattern": r"\b(logout[_-]?all|force[_-]?logout|revoke[-_]?all|revoke[-_]?sessions|invalidate[-_]?all[-_]?sessions|kick[-_]?all)\b", "enabled": True},
    {"reason": "修改系统/服务配置", "pattern": r"\b(systemctl\s+(enable|disable|edit|mask)|iptables\s+-F|ufw\s+(enable|disable|reset)|crontab\s+(-r|-e)|sed\s+-i\s+.*(/etc/|nginx|apache|mysql|postgres))", "enabled": True},
    {"reason": "篡改账户/权限体系", "pattern": r"\b(useradd|userdel|usermod|groupadd|groupdel|chmod\s+-R\s+777\s+/|chown\s+-R\s+[^\s]+\s+/)\b|\b(grant\s+all|revoke\s+all)\b|/admin/users/[^\s]+/role\b", "enabled": True},
    {"reason": "删除或清空关键数据", "pattern": r"\b(drop\s+table|truncate\s+table|delete\s+from\s+\w+\s*;|rm\s+-rf\s+(/|/etc|/var|/usr|/opt|/home|\*)|redis-cli\s+flush(all|db)|clear[-_]?all[-_]?\w+)\b", "enabled": True},
    {"reason": "停止/重启业务服务", "pattern": r"\b(systemctl\s+(stop|restart|reload)\s+\S+|service\s+\S+\s+(stop|restart|reload)|shutdown\b|reboot\b|(pkill|killall)\s+(-f\s+)?(nginx|apache2?|httpd|mysql|mysqld|postgres|postgresql|redis-server|java|node|gunicorn|uwsgi|php-fpm))\b", "enabled": True},
    {"reason": "高并发/DoS 压测", "pattern": r"\b(slowloris|hping3|ab\s+-n\s+\d{5,}|wrk\s+.*(-c\s+\d{3,}|-t\s+\d{2,})|siege\s+.*(-c\s+\d{3,}))\b", "enabled": True},
]


def whitelist_decision(normalized: dict[str, Any], config: GuardPolicyConfig) -> tuple[Decision, str] | None:
    tool_name = str(normalized.get("tool_name") or "").strip().lower()
    whitelist_tools = {item.strip().lower() for item in (config.whitelist_tools or DEFAULT_WHITELIST_TOOLS) if item.strip()}
    if tool_name in whitelist_tools:
        return "ALLOW", f"白名单工具: {tool_name}"
    if tool_name == "bash" and _is_safe_whitelisted_bash(str(normalized.get("command") or ""), config.whitelist_bash_verbs):
        return "ALLOW", "白名单 bash 侦察命令"
    return None


def hard_rule_decision(normalized: dict[str, Any], rules: list[dict[str, object]] | None = None) -> tuple[Decision, str]:
    text = _hard_rule_scan_text(normalized)
    for reason, pattern in compile_hard_block_rules(rules):
        if pattern.search(text):
            return "BLOCK", reason
    return "ALLOW", ""


def _hard_rule_scan_text(normalized: dict[str, Any]) -> str:
    tool_name = str(normalized.get("tool_name", "") or "")
    command = str(normalized.get("command", "") or "")
    path = str(normalized.get("path", "") or "")
    if tool_name.strip().lower() == "bash":
        command = _strip_shell_heredoc_bodies(command)
    return f"{tool_name} {command} {path}"


_HEREDOC_RE = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<marker>[A-Za-z0-9_./-]+)(?P=quote)")


def _strip_shell_heredoc_bodies(command: str) -> str:
    """Remove here-doc bodies before deterministic hard-rule regex scanning.

    Workers often write markdown reports with `cat > file <<EOF`. Those report
    bodies can legitimately mention blocked examples such as `HTTP DELETE` or
    `systemctl restart nginx`. Hard rules should evaluate the shell operation
    being executed, not quoted documentation embedded in a here-doc. Lines after
    the here-doc terminator are kept, so a real dangerous command following the
    write is still caught.
    """
    if "<<" not in command:
        return command

    kept: list[str] = []
    pending_markers: list[str] = []
    for line in command.splitlines():
        if pending_markers:
            terminator = line.lstrip("\t").strip()
            if terminator == pending_markers[0]:
                pending_markers.pop(0)
                kept.append(f"# heredoc {terminator} end")
            continue

        kept.append(line)
        for match in _HEREDOC_RE.finditer(line):
            marker = match.group("marker").strip()
            if marker:
                pending_markers.append(marker)
    return "\n".join(kept)


def compile_hard_block_rules(rules: list[dict[str, object]] | None = None) -> list[tuple[str, re.Pattern[str]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for raw in rules or DEFAULT_HARD_BLOCK_RULES:
        if raw.get("enabled", True) is False:
            continue
        reason = str(raw.get("reason") or "硬规则阻断").strip() or "硬规则阻断"
        pattern = str(raw.get("pattern") or "").strip()
        if not pattern:
            continue
        try:
            compiled.append((reason, re.compile(pattern, re.I)))
        except re.error:
            continue
    return compiled


def evaluate_guard_event(
    *,
    tool_name: str,
    input_payload: Any,
    metadata: dict[str, Any] | None = None,
    config: GuardPolicyConfig | None = None,
    api_key: str | None = None,
) -> GuardEvaluation:
    cfg = config or GuardPolicyConfig(
        mode=(os.getenv("AXIOM_GUARD_MODE") or "enforce"),  # type: ignore[arg-type]
        base_url=(os.getenv("AXIOM_GUARD_MODEL_BASE_URL") or "https://api.siliconflow.cn/v1"),
        model_name=(os.getenv("AXIOM_GUARD_MODEL") or "Qwen/Qwen3.5-9B"),
        timeout_seconds=int(os.getenv("AXIOM_GUARD_MODEL_TIMEOUT", "3") or "3"),
    )
    normalized = normalize_tool_input(tool_name, input_payload)
    if cfg.mode == "off":
        return GuardEvaluation("ALLOW", "", "disabled", normalized, model_name=cfg.model_name)

    white = whitelist_decision(normalized, cfg)
    if white is not None:
        decision, reason = white
        return GuardEvaluation(decision, reason, "whitelist", normalized, model_name=cfg.model_name)

    hard_decision, hard_reason = hard_rule_decision(normalized, cfg.hard_block_rules)
    if hard_decision == "BLOCK":
        return GuardEvaluation("BLOCK", hard_reason, "hard_rule", normalized)

    resolved_key = api_key or os.getenv("AXIOM_GUARD_API_KEY") or os.getenv("SILICONFLOW_API_KEY")
    if not resolved_key:
        return GuardEvaluation("ALLOW", "guard fail-open: missing api key", "fail_open", normalized, model_name=cfg.model_name)

    payload = {
        "tool_name": tool_name,
        "input": input_payload,
        "normalized": normalized,
        "metadata": metadata or {},
    }
    started = time.perf_counter()
    try:
        response = requests.post(
            f"{cfg.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {resolved_key}", "Content-Type": "application/json"},
            json={
                "model": cfg.model_name,
                "messages": [
                    {"role": "system", "content": (cfg.system_prompt or MODEL_SYSTEM_PROMPT).strip() or MODEL_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                "temperature": 0,
                "max_tokens": 64,
                "enable_thinking": False,
            },
            timeout=max(1, int(cfg.timeout_seconds or 3)),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        data = response.json()
        raw = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else ""
        detail = ""
        if exc.response is not None:
            detail = (exc.response.text or "").replace("\n", " ")[:160]
        reason = f"guard fail-open: HTTP {status} {detail}".strip()
        return GuardEvaluation("ALLOW", reason, "fail_open", normalized, model_name=cfg.model_name)
    except Exception as exc:
        return GuardEvaluation("ALLOW", f"guard fail-open: {type(exc).__name__}", "fail_open", normalized, model_name=cfg.model_name)

    first_line = raw.splitlines()[0].strip() if raw else ""
    if first_line == "ALLOW":
        return GuardEvaluation("ALLOW", "", "model", normalized, model_name=cfg.model_name, model_latency_ms=latency_ms, raw_model_output=raw)
    if first_line.startswith("BLOCK:"):
        reason = first_line.split(":", 1)[1].strip()[:30] or "小模型判定阻断"
        return GuardEvaluation("BLOCK", reason, "model", normalized, model_name=cfg.model_name, model_latency_ms=latency_ms, raw_model_output=raw)
    return GuardEvaluation("ALLOW", "guard fail-open: invalid model output", "fail_open", normalized, model_name=cfg.model_name, model_latency_ms=latency_ms, raw_model_output=raw)


def new_hook_id() -> str:
    return "hook_" + uuid.uuid4().hex[:16]


def _is_safe_whitelisted_bash(command: str, whitelist_verbs: list[str]) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    dangerous_fragments = ["|", ";", ">", "<", "`", "$(", "${", "\n", "\r", "||"]
    if any(fragment in text for fragment in dangerous_fragments):
        return False
    # Single & is shell control; allow && only as segment separator.
    if "&" in text.replace("&&", ""):
        return False
    verbs = {item.strip().lower() for item in (whitelist_verbs or DEFAULT_WHITELIST_BASH_VERBS) if item.strip()}
    for segment in [part.strip() for part in text.split("&&") if part.strip()]:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            return False
        verb = PurePosixPath(tokens[0]).name.lower()
        if verb not in verbs:
            return False
        if verb == "find" and any(token in {"-delete", "-exec", "-execdir"} for token in tokens[1:]):
            return False
        if verb == "ip" and any(token in {"set", "add", "del", "delete", "replace", "flush"} for token in tokens[1:]):
            return False
        if verb == "ifconfig" and any(token.lower() in {"up", "down", "netmask", "broadcast", "mtu"} for token in tokens[1:]):
            return False
        if verb in {"env", "printenv"} and len(tokens) > 1 and any("=" in token for token in tokens[1:]):
            return False
    return True
