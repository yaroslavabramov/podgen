from __future__ import annotations

from typing import Any

from app.generation_v2.runtime_tool_output import MODEL_HIDDEN_TOOL_KEYS

CHECKPOINT_STRING_MAX_CHARS = 2200
CHECKPOINT_LIST_MAX_ITEMS = 24
MODEL_TOOL_MAX_DEPTH = 5


def compact_checkpoint_value(
    value: Any,
    *,
    max_string_chars: int = CHECKPOINT_STRING_MAX_CHARS,
    max_list_items: int = CHECKPOINT_LIST_MAX_ITEMS,
    max_depth: int = MODEL_TOOL_MAX_DEPTH,
    depth: int = 0,
) -> tuple[Any, bool]:
    if depth > max_depth:
        return "[nested checkpoint state omitted]", True
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value, False
        return value[:max_string_chars].rstrip() + "...", True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, list):
        omitted = len(value) > max_list_items
        items = []
        for item in value[:max_list_items]:
            compact_item, item_omitted = compact_checkpoint_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                depth=depth + 1,
            )
            items.append(compact_item)
            omitted = omitted or item_omitted
        return items, omitted
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        omitted = False
        for key, item in value.items():
            key_text = str(key)
            if key_text in MODEL_HIDDEN_TOOL_KEYS:
                omitted = True
                continue
            compact_item, item_omitted = compact_checkpoint_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                depth=depth + 1,
            )
            compact[key_text] = compact_item
            omitted = omitted or item_omitted
        return compact, omitted
    if hasattr(value, "model_dump"):
        return compact_checkpoint_value(
            value.model_dump(mode="json"),
            max_string_chars=max_string_chars,
            max_list_items=max_list_items,
            max_depth=max_depth,
            depth=depth,
        )
    return str(value), False


def collect_tool_ids(value: Any) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {
        "evidence_ids": [],
        "source_refs": [],
        "version_refs": [],
    }

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                text = str(item)
                if key in {"evidence_id", "id"} and len(text) < 80:
                    _append_unique(collected["evidence_ids"], text)
                elif key == "source_ref" and len(text) < 80:
                    _append_unique(collected["source_refs"], text)
                elif key == "version_ref" and len(text) < 80:
                    _append_unique(collected["version_refs"], text)
                elif key in {"evidence_ids", "source_refs", "version_refs"} and isinstance(item, list):
                    target = collected.get(key)
                    if target is not None:
                        for value_item in item[:20]:
                            if len(str(value_item)) < 80:
                                _append_unique(target, str(value_item))
                visit(item)
        elif isinstance(node, list):
            for item in node[:100]:
                visit(item)

    visit(value)
    return {key: values[:20] for key, values in collected.items() if values}


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)
