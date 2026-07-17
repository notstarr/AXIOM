from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from axiom.guard.policy import DEFAULT_HARD_BLOCK_RULES, MODEL_SYSTEM_PROMPT
from axiom.server.services import utcnow

JudgeMode = Literal["off", "observe", "guarded", "enforce"]

DEFAULT_JUDGE_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_JUDGE_MODEL = "Qwen/Qwen3.5-9B"
DEFAULT_JUDGE_TIMEOUT_SECONDS = 3
DEFAULT_JUDGE_SYSTEM_PROMPT = MODEL_SYSTEM_PROMPT
DEFAULT_JUDGE_HARD_BLOCK_RULES = DEFAULT_HARD_BLOCK_RULES
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


@dataclass(slots=True)
class JudgeSettingsData:
    enabled_default: bool = False
    mode: JudgeMode = "enforce"
    base_url: str = DEFAULT_JUDGE_BASE_URL
    model: str = DEFAULT_JUDGE_MODEL
    timeout_seconds: int = DEFAULT_JUDGE_TIMEOUT_SECONDS
    whitelist_tools: list[str] = field(default_factory=lambda: list(DEFAULT_WHITELIST_TOOLS))
    whitelist_bash_verbs: list[str] = field(default_factory=lambda: list(DEFAULT_WHITELIST_BASH_VERBS))
    system_prompt: str = DEFAULT_JUDGE_SYSTEM_PROMPT
    hard_block_rules: list[dict[str, object]] = field(default_factory=lambda: [dict(rule) for rule in DEFAULT_JUDGE_HARD_BLOCK_RULES])
    updated_at: str = ""


def load_judge_settings(conn: sqlite3.Connection) -> JudgeSettingsData:
    row = conn.execute("SELECT * FROM judge_settings WHERE singleton_id = 1").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO judge_settings (singleton_id) VALUES (1)")
        row = conn.execute("SELECT * FROM judge_settings WHERE singleton_id = 1").fetchone()
    if row is None:
        return JudgeSettingsData()
    mode = str(row["mode"] or "enforce").strip().lower()
    if mode not in {"off", "observe", "guarded", "enforce"}:
        mode = "enforce"
    return JudgeSettingsData(
        enabled_default=bool(row["enabled_default"]),
        mode=mode,  # type: ignore[arg-type]
        base_url=(row["base_url"] or DEFAULT_JUDGE_BASE_URL).strip().rstrip("/") or DEFAULT_JUDGE_BASE_URL,
        model=(row["model"] or DEFAULT_JUDGE_MODEL).strip() or DEFAULT_JUDGE_MODEL,
        timeout_seconds=max(1, int(row["timeout_seconds"] or DEFAULT_JUDGE_TIMEOUT_SECONDS)),
        whitelist_tools=_parse_string_list(row["whitelist_tools_json"], DEFAULT_WHITELIST_TOOLS),
        whitelist_bash_verbs=_parse_string_list(row["whitelist_bash_verbs_json"], DEFAULT_WHITELIST_BASH_VERBS),
        system_prompt=(row["system_prompt"] or DEFAULT_JUDGE_SYSTEM_PROMPT).strip() or DEFAULT_JUDGE_SYSTEM_PROMPT,
        hard_block_rules=_parse_hard_rules(row["hard_block_rules_json"], DEFAULT_JUDGE_HARD_BLOCK_RULES),
        updated_at=row["updated_at"] or "",
    )


def save_judge_settings(conn: sqlite3.Connection, settings: JudgeSettingsData) -> JudgeSettingsData:
    normalized = JudgeSettingsData(
        enabled_default=bool(settings.enabled_default),
        mode=settings.mode if settings.mode in {"off", "observe", "guarded", "enforce"} else "enforce",
        base_url=(settings.base_url or DEFAULT_JUDGE_BASE_URL).strip().rstrip("/") or DEFAULT_JUDGE_BASE_URL,
        model=(settings.model or DEFAULT_JUDGE_MODEL).strip() or DEFAULT_JUDGE_MODEL,
        timeout_seconds=max(1, int(settings.timeout_seconds or DEFAULT_JUDGE_TIMEOUT_SECONDS)),
        whitelist_tools=_normalize_list(settings.whitelist_tools, DEFAULT_WHITELIST_TOOLS),
        whitelist_bash_verbs=_normalize_list(settings.whitelist_bash_verbs, DEFAULT_WHITELIST_BASH_VERBS),
        system_prompt=(settings.system_prompt or DEFAULT_JUDGE_SYSTEM_PROMPT).strip() or DEFAULT_JUDGE_SYSTEM_PROMPT,
        hard_block_rules=_normalize_hard_rules(settings.hard_block_rules, DEFAULT_JUDGE_HARD_BLOCK_RULES),
        updated_at=utcnow(),
    )
    conn.execute(
        """
        INSERT INTO judge_settings (
            singleton_id, enabled_default, mode, base_url, model, timeout_seconds,
            whitelist_tools_json, whitelist_bash_verbs_json, system_prompt, hard_block_rules_json, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            enabled_default = excluded.enabled_default,
            mode = excluded.mode,
            base_url = excluded.base_url,
            model = excluded.model,
            timeout_seconds = excluded.timeout_seconds,
            whitelist_tools_json = excluded.whitelist_tools_json,
            whitelist_bash_verbs_json = excluded.whitelist_bash_verbs_json,
            system_prompt = excluded.system_prompt,
            hard_block_rules_json = excluded.hard_block_rules_json,
            updated_at = excluded.updated_at
        """,
        (
            1 if normalized.enabled_default else 0,
            normalized.mode,
            normalized.base_url,
            normalized.model,
            normalized.timeout_seconds,
            json.dumps(normalized.whitelist_tools, ensure_ascii=False, separators=(",", ":")),
            json.dumps(normalized.whitelist_bash_verbs, ensure_ascii=False, separators=(",", ":")),
            normalized.system_prompt,
            json.dumps(normalized.hard_block_rules, ensure_ascii=False, separators=(",", ":")),
            normalized.updated_at,
        ),
    )
    return normalized


def _parse_string_list(raw: Any, default: list[str]) -> list[str]:
    if not raw:
        return list(default)
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return list(default)
    if not isinstance(parsed, list):
        return list(default)
    return _normalize_list([str(item) for item in parsed], default)


def _normalize_list(values: list[str], default: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized or list(default)


def _parse_hard_rules(raw: Any, default: list[dict[str, object]]) -> list[dict[str, object]]:
    if not raw:
        return [dict(rule) for rule in default]
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return [dict(rule) for rule in default]
    if not isinstance(parsed, list):
        return [dict(rule) for rule in default]
    return _normalize_hard_rules(parsed, default)


def _normalize_hard_rules(values: list[dict[str, object]], default: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for raw in values:
        if not isinstance(raw, dict):
            continue
        reason = str(raw.get("reason") or "").strip()
        pattern = str(raw.get("pattern") or "").strip()
        if not reason or not pattern:
            continue
        normalized.append({"reason": reason, "pattern": pattern, "enabled": bool(raw.get("enabled", True))})
    return normalized or [dict(rule) for rule in default]
