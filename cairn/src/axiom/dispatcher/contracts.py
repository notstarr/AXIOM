from __future__ import annotations

from typing import Any

from axiom.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    has_vulns = "vulns" in keys
    if has_vulns:
        keys.remove("vulns")
        vulns = payload.get("vulns")
        if not isinstance(vulns, list):
            return False
        if not keys:
            return True
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    if not _is_dict(payload.get("fact")):
        return False
    complete = payload.get("complete")
    return complete is None or _is_dict(complete)


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) == {"description"}


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    has_vulns = "vulns" in data
    normalized_data = dict(data)
    normalized_data.pop("vulns", None)
    complete = normalized_data.get("complete")
    intents = normalized_data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = normalized_data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is None and intents is None and not normalized_data:
        if has_vulns:
            return "noop", None
        # open_intents_empty + empty data = depth mode "all covered" signal
        # (prompt says: all tested, no chains → return {"accepted": true, "data": {}})
        return "noop", None
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    # open_intents_empty + no complete/intents = noop (depth mode: plan exhausted)
    return "noop", None


def extract_reason_vulns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return []
    source = data if accepted is True else payload
    if not isinstance(source, dict):
        return []
    vulns = source.get("vulns")
    if vulns is None:
        return []
    if not isinstance(vulns, list):
        raise ValueError("vulns must be an array")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(vulns):
        if not isinstance(item, dict):
            raise ValueError(f"vulns[{index}] must be an object")
        required = ["title", "description", "impact"]
        for key in required:
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"vulns[{index}].{key} is required")
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(f"vulns[{index}].evidence must be an array")
        steps = item.get("reproduction_steps", [])
        if not isinstance(steps, list):
            raise ValueError(f"vulns[{index}].reproduction_steps must be an array")
        status = item.get("status", "confirmed")
        if not isinstance(status, str):
            raise ValueError(f"vulns[{index}].status must be a string")
        status = status.strip().lower() or "confirmed"
        if status not in {"confirmed", "pending", "rejected", "ignored"}:
            raise ValueError(f"vulns[{index}].status is invalid")
        normalized.append(
            {
                "title": item["title"].strip(),
                "description": item["description"].strip(),
                "prerequisites": str(item.get("prerequisites", "")).strip(),
                "impact": item["impact"].strip(),
                "evidence": [str(x).strip() for x in evidence if str(x).strip()],
                "reproduction_steps": [str(x).strip() for x in steps if str(x).strip()],
                "status": status,
                "source": "reason",
            }
        )
    return normalized


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        return "fact", result
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", description.strip()


def validate_plan_payload(payload: dict[str, Any]) -> tuple[str, list[str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        # Plan doesn't require accepted wrapper
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    facts = data.get("facts")
    if not isinstance(facts, list) or len(facts) == 0:
        raise ValueError("facts must be a non-empty array")
    descriptions: list[str] = []
    for i, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise ValueError(f"facts[{i}] must be an object")
        desc = fact.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise ValueError(f"facts[{i}].description is required")
        descriptions.append(desc.strip())
    return "facts", descriptions
