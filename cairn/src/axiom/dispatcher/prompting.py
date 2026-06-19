from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any


def load_prompt(group: str, name: str) -> str:
    return resources.files("axiom.dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    if not replacements:
        return template
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in replacements) + r")\}")
    return pattern.sub(lambda m: replacements[m.group(1)], template)


def format_fact_ids(fact_ids: list[str]) -> str:
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    return format_json_block(hints)


def format_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
