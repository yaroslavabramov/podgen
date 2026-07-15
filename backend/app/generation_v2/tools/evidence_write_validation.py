from __future__ import annotations

import json
from typing import Any


SOURCE_MAP_WRITE_MAX_CHARS = 9_000

SOURCE_MAP_WRITE_TEXT_LIMITS: dict[str, dict[str, int]] = {
    "select_evidence": {
        "evidence_id": 128,
        "purpose": 240,
        "role": 240,
        "rationale": 3_000,
    },
    "upsert_source_card": {"source_ref": 128, "source_id": 128, "summary": 5_000},
    "create_quote_card": {
        "evidence_id": 128,
        "source_id": 128,
        "source_version_id": 128,
        "quote": 7_000,
        "locator": 1_000,
        "narrative_use": 1_000,
        "confidence_reason": 1_000,
    },
    "record_source_relationship": {
        "source_ref": 128,
        "source_id": 128,
        "related_source_ref": 128,
        "related_source_id": 128,
        "relationship": 64,
        "rationale": 5_000,
    },
    "record_source_cluster": {"label": 500, "rationale": 5_000},
    "record_gap": {"kind": 120, "description": 7_000},
    "resolve_gap": {"gap_ref": 32, "basis_ref": 256, "reason": 3_000},
    "resolve_source_map_repair": {"repair_ref": 32, "reason": 3_000},
    "finalize_source_map": {"summary": 7_000},
}

SOURCE_MAP_WRITE_LIST_LIMITS: dict[str, dict[str, tuple[int, int]]] = {
    "upsert_source_card": {
        "useful_for": (12, 600),
        "evidence_ids": (40, 128),
        "gaps_or_cautions": (12, 600),
    },
    "record_source_relationship": {"evidence_ids": (40, 128)},
    "record_source_cluster": {
        "source_refs": (80, 128),
        "source_ids": (80, 128),
        "primary_source_refs": (80, 128),
        "secondary_source_refs": (80, 128),
        "duplicate_source_refs": (80, 128),
        "evidence_ids": (40, 128),
    },
    "record_gap": {
        "source_refs": (80, 128),
        "source_ids": (80, 128),
        "question_ids": (80, 128),
        "evidence_ids": (40, 128),
    },
}


def validate_source_map_write_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    for field, max_length in SOURCE_MAP_WRITE_TEXT_LIMITS.get(tool_name, {}).items():
        value = str(args.get(field) or "")
        if len(value) > max_length:
            return _too_long(field, max_length, len(value))

    for field, (max_items, max_item_length) in SOURCE_MAP_WRITE_LIST_LIMITS.get(tool_name, {}).items():
        raw_values = args.get(field) or []
        if not isinstance(raw_values, list):
            return {
                "accepted": False,
                "error": f"semantic_field_invalid: field={field} expected=array",
            }
        if len(raw_values) > max_items:
            return {
                "accepted": False,
                "error": (
                    f"semantic_collection_too_large: field={field} "
                    f"max_items={max_items} actual_items={len(raw_values)}"
                ),
            }
        for index, item in enumerate(raw_values):
            item_length = len(str(item or ""))
            if item_length > max_item_length:
                return _too_long(f"{field}[{index}]", max_item_length, item_length)

    payload_length = len(json.dumps(args, ensure_ascii=False, default=str))
    if payload_length > SOURCE_MAP_WRITE_MAX_CHARS:
        return {
            "accepted": False,
            "error": (
                f"semantic_payload_too_long: tool={tool_name} "
                f"max_length={SOURCE_MAP_WRITE_MAX_CHARS} actual_length={payload_length}"
            ),
        }
    return None


def _too_long(field: str, max_length: int, actual_length: int) -> dict[str, Any]:
    return {
        "accepted": False,
        "error": (
            f"semantic_field_too_long: field={field} "
            f"max_length={max_length} actual_length={actual_length}"
        ),
    }


__all__ = [
    "SOURCE_MAP_WRITE_LIST_LIMITS",
    "SOURCE_MAP_WRITE_MAX_CHARS",
    "SOURCE_MAP_WRITE_TEXT_LIMITS",
    "validate_source_map_write_args",
]
