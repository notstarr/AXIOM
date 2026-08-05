from __future__ import annotations

from copy import deepcopy
from typing import Any


PROPERTY_ORDER_KEY = "x-axiom-property-order"


def annotate_input_schema(schema: object) -> dict[str, Any]:
    """Persist JSON Schema property order explicitly before writing JSONB.

    PostgreSQL JSONB deliberately does not preserve object-key order.  MCP
    parameter order is nevertheless part of AXIOM's human/model-facing tool
    signature, so retain it in a private schema extension.
    """

    if not isinstance(schema, dict):
        return {}
    result = deepcopy(schema)
    properties = result.get("properties")
    if not isinstance(properties, dict):
        result.pop(PROPERTY_ORDER_KEY, None)
        return result

    supplied = result.get(PROPERTY_ORDER_KEY)
    requested = supplied if isinstance(supplied, list) else list(properties)
    order: list[str] = []
    for item in requested:
        key = str(item)
        if key in properties and key not in order:
            order.append(key)
    order.extend(key for key in properties if key not in order)
    result[PROPERTY_ORDER_KEY] = order
    return result


def public_input_schema(schema: object) -> dict[str, Any]:
    """Return the public schema with properties restored to persisted order."""

    annotated = annotate_input_schema(schema)
    properties = annotated.get("properties")
    order = annotated.pop(PROPERTY_ORDER_KEY, None)
    if isinstance(properties, dict) and isinstance(order, list):
        annotated["properties"] = {
            key: properties[key]
            for key in order
            if isinstance(key, str) and key in properties
        }
    return annotated


def ordered_property_names(schema: object) -> list[str]:
    annotated = annotate_input_schema(schema)
    properties = annotated.get("properties")
    order = annotated.get(PROPERTY_ORDER_KEY)
    if not isinstance(properties, dict) or not isinstance(order, list):
        return []
    return [key for key in order if isinstance(key, str) and key in properties]
