from __future__ import annotations

import json
from typing import Any


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Pydantic schema for OpenAI strict structured outputs."""
    schema = json.loads(json.dumps(schema))
    schema.pop("title", None)
    strict_object(schema)
    return schema


def strict_object(node: Any) -> None:
    if not isinstance(node, dict):
        return
    node.pop("default", None)
    node.pop("examples", None)
    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties") or {}
        node["additionalProperties"] = False
        node["required"] = list(properties.keys())
        for child in properties.values():
            strict_object(child)
    for key in ("items", "anyOf", "oneOf", "allOf", "$defs"):
        value = node.get(key)
        if isinstance(value, list):
            for item in value:
                strict_object(item)
        elif isinstance(value, dict):
            for item in (value.values() if key == "$defs" else [value]):
                strict_object(item)
