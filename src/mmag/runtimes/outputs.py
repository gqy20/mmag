"""Deterministic normalization for schema-constrained model output."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def repair_structured_output(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """Decode model-stringified JSON containers only where the schema requires them."""
    repaired, count = _repair_schema_value(dict(value), schema, schema)
    return (dict(repaired) if isinstance(repaired, Mapping) else dict(value), count)


def _repair_schema_value(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
) -> tuple[Any, int]:
    resolved = _resolve_local_schema(schema, root_schema)
    variants = [
        item
        for keyword in ("oneOf", "anyOf")
        for item in resolved.get(keyword, ())
        if isinstance(item, Mapping)
    ]
    allowed_types = _schema_types(resolved, root_schema)
    repaired = 0
    value, decoded = _decode_container(value, allowed_types)
    repaired += decoded

    if variants:
        matching = [
            variant
            for variant in variants
            if _json_type(value) in _schema_types(variant, root_schema)
        ]
        if matching:
            value, nested = _repair_schema_value(value, matching[0], root_schema)
            return value, repaired + nested

    if isinstance(value, Mapping):
        return _repair_object(value, resolved, root_schema, repaired)
    items = resolved.get("items")
    if isinstance(value, (list, tuple)) and isinstance(items, Mapping):
        output = []
        for item in value:
            converted, nested = _repair_schema_value(item, items, root_schema)
            output.append(converted)
            repaired += nested
        return output, repaired
    return value, repaired


def _decode_container(value: Any, allowed_types: set[str]) -> tuple[Any, int]:
    if not isinstance(value, str) or "string" in allowed_types:
        return value, 0
    container_types = allowed_types & {"object", "array"}
    if not container_types:
        return value, 0
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value, 0
    return (decoded, 1) if _json_type(decoded) in container_types else (value, 0)


def _repair_object(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    repaired: int,
) -> tuple[dict[str, Any], int]:
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties")
    output = dict(value)
    for key, item in value.items():
        field_schema = properties.get(key) if isinstance(properties, Mapping) else None
        if not isinstance(field_schema, Mapping) and isinstance(additional, Mapping):
            field_schema = additional
        if isinstance(field_schema, Mapping):
            output[key], nested = _repair_schema_value(item, field_schema, root_schema)
            repaired += nested
    return output, repaired


def _resolve_local_schema(
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    current: Any = root_schema
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            return schema
        current = current[part]
    return current if isinstance(current, Mapping) else schema


def _schema_types(
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
) -> set[str]:
    resolved = _resolve_local_schema(schema, root_schema)
    declared = resolved.get("type")
    types = (
        {declared}
        if isinstance(declared, str)
        else {item for item in declared or () if isinstance(item, str)}
    )
    for keyword in ("oneOf", "anyOf"):
        for variant in resolved.get(keyword, ()):
            if isinstance(variant, Mapping):
                types.update(_schema_types(variant, root_schema))
    if not types and "properties" in resolved:
        types.add("object")
    if not types and "items" in resolved:
        types.add("array")
    return types


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "unknown"
