from __future__ import annotations

import ipaddress

from axiom.modules.tasks.origin import parse_origin
from axiom.modules.tasks.schemas import TaskScope


def normalize_intent_target(raw: str) -> str:
    return parse_origin(raw).host


def target_allowed(scope: TaskScope, raw_target: str) -> bool:
    if not scope.enabled:
        return True
    target = normalize_intent_target(raw_target)
    if any(_matches(pattern, target) for pattern in scope.exclude):
        return False
    return bool(scope.targets) and any(_matches(pattern, target) for pattern in scope.targets)


def _matches(pattern: str, target: str) -> bool:
    normalized = pattern.strip().lower().rstrip(".")
    if not normalized:
        return False
    if normalized.startswith("*."):
        suffix = normalized[2:]
        return target.endswith(f".{suffix}") and target != suffix
    normalized = normalize_intent_target(normalized)
    try:
        return ipaddress.ip_address(normalized) == ipaddress.ip_address(target)
    except ValueError:
        return normalized == target
