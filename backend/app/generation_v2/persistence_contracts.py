from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def semantic_field_error(
    values: Mapping[str, Any],
    field_limits: Mapping[str, int],
    field_names: Iterable[str],
) -> dict[str, Any] | None:
    for field in field_names:
        value = values.get(field)
        if value is None:
            continue
        text = str(value)
        limit = field_limits.get(field)
        if limit and len(text) > limit:
            return {
                "accepted": False,
                "error": "semantic_field_too_long",
                "field": field,
                "max_length": limit,
                "actual_length": len(text),
            }
    return None


def semantic_list_error(
    *,
    field: str,
    values: Sequence[Any],
    max_items: int | None = None,
    item_limit: int | None = None,
) -> dict[str, Any] | None:
    if max_items is not None and len(values) > max_items:
        return {
            "accepted": False,
            "error": "semantic_list_too_long",
            "field": field,
            "max_items": max_items,
            "actual_items": len(values),
        }
    if item_limit is None:
        return None
    for index, value in enumerate(values):
        text = str(value)
        if len(text) > item_limit:
            return {
                "accepted": False,
                "error": "semantic_field_too_long",
                "field": field,
                "item_index": index,
                "max_length": item_limit,
                "actual_length": len(text),
            }
    return None
