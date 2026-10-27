"""Parsing helpers shared by the worker CLI and the API layer.

Both sides validate with the same code so a worker gets the error locally (fast, and
with a message it can act on) instead of as an HTTP 422 after a full round trip.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from axiom.modules.reproduction.schemas import Reproduction


def parse_reproduction(raw: str) -> Reproduction:
    """Parse and fully validate a reproduction JSON document.

    Raises ``ValueError`` with a message meant for the worker: what was wrong and how
    to fix it.  ``json.JSONDecodeError`` is translated too, because a truncated heredoc
    is the most common way this fails in practice.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("复现内容为空")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"复现 JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）；"
            "报文里的换行要写成 \\n 转义，不要直接换行"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("复现 JSON 顶层必须是对象（包含 entry_point / steps 等字段）")
    try:
        reproduction = Reproduction.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from exc
    return reproduction


def load_reproduction_file(path: str) -> Reproduction:
    """Read a reproduction from ``path``; ``-`` reads standard input.

    Standard input matters because the packet text contains newlines and quoting a
    JSON document into a shell argument is where workers get it wrong.
    """
    target = (path or "").strip()
    if not target:
        raise ValueError("复现文件路径为空")
    if target == "-":
        return parse_reproduction(sys.stdin.read())
    candidate = Path(target)
    if not candidate.is_file():
        raise ValueError(f"复现文件不存在：{target}")
    return parse_reproduction(candidate.read_text(encoding="utf-8"))


def _format_validation_error(exc: ValidationError) -> str:
    """Flatten pydantic errors into one actionable line per field.

    Pydantic's default rendering buries the custom Chinese messages under loc tuples;
    a worker reading CLI output needs the field name and the instruction.
    """
    lines: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "(顶层)"
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"{location}: {message}")
    return "复现内容不合法：" + "；".join(lines)
