from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
FENCE_STRIP_RE = re.compile(r"^```(?:json)?\s*|```$", re.IGNORECASE)
# Deepseek reasoning often wraps output like:  content ... ```json {...} ```
# The regex above handles it, but also try stripping the outermost fence markers
# from the raw text as a fallback.

def _strip_outer_fence(text: str) -> str:
    """Attempt to strip a single surrounding code fence from text."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        inner = stripped[3:-3].strip()
        if inner.lower().startswith("json"):
            inner = inner[4:].strip()
        return inner
    return text


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        parsed_candidates: list[tuple[int, int, dict[str, Any]]] = []
        for start in _object_start_positions(segment):
            try:
                parsed, end = decoder.raw_decode(segment[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                parsed_candidates.append((start, end, parsed))
        if parsed_candidates:
            return _best_parsed_object(parsed_candidates)

    raise ValueError("no JSON object found in output")


def _candidate_segments(text: str) -> list[str]:
    segments: list[str] = []
    # 1) Extract from code fences (handles both \n and inline)
    for match in FENCED_BLOCK_RE.finditer(text):
        segments.append(match.group(1).strip())
    # 2) Strip single outermost fence as fallback (deepseek sometimes wraps the whole output)
    stripped = _strip_outer_fence(text)
    if stripped != text:
        segments.append(stripped)
    # 3) Raw text as last resort
    segments.append(text.strip())
    # 4) Find last { ... } pair spanning the tail of the output (common for long reasoning outputs)
    first_brace = text.rfind("\n{")
    if first_brace >= 0:
        tail = text[first_brace + 1:].strip()
        segments.append(tail)
    return segments


def _object_start_positions(text: str) -> list[int]:
    return [index for index, char in reversed(list(enumerate(text))) if char == "{"]


def _best_parsed_object(candidates: list[tuple[int, int, dict[str, Any]]]) -> dict[str, Any]:
    accepted_candidates = [item for item in candidates if "accepted" in item[2]]
    if accepted_candidates:
        return max(accepted_candidates, key=lambda item: (item[0], item[1]))[2]
    return candidates[0][2]
