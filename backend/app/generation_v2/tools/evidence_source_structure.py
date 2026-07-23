from __future__ import annotations

import json
from typing import Any

from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.tools.evidence_text_windows import (
    heading_markers,
    page_markers,
    timestamp_markers,
)
from app.generation_v2.tools.evidence_window_refs import create_window_ref


_LABEL_PREVIEW_CHARS = 160
_UNSTRUCTURED_RANGE_CHARS = 20_000
_NAVIGATION_WINDOW_CHARS = 3_000


def build_source_structure_page(
    *,
    text: str,
    source_version_id: Any,
    allow_timestamps: bool,
    cursor: str,
    max_items: int,
    delivery_receipt_ref: str = "",
) -> dict[str, Any]:
    if cursor and not cursor.isdigit():
        return {
            "found": False,
            "error": "invalid_source_structure_cursor",
            "next_action": "Retry inspect_source with cursor=''.",
        }
    offset = int(cursor or 0)
    pages = page_markers(text)
    page_offsets = {int(item["start"]) for item in pages}
    headings = [item for item in heading_markers(text) if int(item["start"]) not in page_offsets]
    timestamps = timestamp_markers(text) if allow_timestamps else []
    outline = [
        *[
            {
                "kind": "page",
                "label": f"page {item['page']}",
                "offset": int(item["start"]),
            }
            for item in pages
        ],
        *[
            {
                "kind": "heading",
                "label": str(item["heading"]),
                "offset": int(item["start"]),
            }
            for item in headings
        ],
        *[
            {
                "kind": "timestamp",
                "label": str(item["timestamp"]),
                "offset": int(item["start"]),
            }
            for item in timestamps
        ],
    ]
    outline.sort(key=lambda item: (item["offset"], item["kind"], item["label"]))
    if outline and int(outline[0]["offset"]) > 0:
        outline.insert(
            0,
            {
                "kind": "text_range",
                "label": f"chars 1-{int(outline[0]['offset'])}",
                "offset": 0,
            },
        )
    if not outline:
        outline = [
            {
                "kind": "text_range",
                "label": f"chars {start + 1}-{min(len(text), start + _UNSTRUCTURED_RANGE_CHARS)}",
                "offset": start,
            }
            for start in range(0, len(text), _UNSTRUCTURED_RANGE_CHARS)
        ]
    total = len(outline)
    page = outline[offset : offset + max_items]
    items: list[dict[str, Any]] = []
    for index, item in enumerate(page, start=offset):
        start = int(item["offset"])
        section_end = int(outline[index + 1]["offset"]) if index + 1 < total else len(text)
        navigation_end = min(len(text), max(start + 1, min(section_end, start + _NAVIGATION_WINDOW_CHARS)))
        label = str(item["label"])
        items.append(
            {
                "kind": item["kind"],
                "label_preview": label[:_LABEL_PREVIEW_CHARS],
                "label_chars": len(label),
                "label_omitted_chars": max(0, len(label) - _LABEL_PREVIEW_CHARS),
                "start_offset": start,
                "end_offset": section_end,
                "section_chars": max(0, section_end - start),
                "navigation_window_range": {
                    "start_offset": start,
                    "end_offset": navigation_end,
                },
                "navigation_window_omitted_chars": max(0, section_end - navigation_end),
                "window_ref": create_window_ref(
                    source_version_id=source_version_id,
                    text=text,
                    start_offset=start,
                    end_offset=navigation_end,
                ),
            }
        )
    payload = {
        "found": bool(items),
        "items": items,
        "counts": {
            "pages": len(pages),
            "headings": len(headings),
            "timestamps": len(timestamps),
            "coarse_text_ranges": sum(1 for item in outline if item["kind"] == "text_range"),
        },
        "total_chars": len(text),
        "total_count": total,
        "cursor": cursor,
        "content_included": False,
        "next_action": "Use read_source for actual reading; window_ref is only for a targeted exact follow-up.",
        **({"structure_receipt_ref": delivery_receipt_ref} if delivery_receipt_ref else {}),
    }
    requested_count = len(items)
    while items:
        next_offset = offset + len(items)
        candidate = {
            **payload,
            "items": items,
            "next_cursor": str(next_offset) if next_offset < total else "",
            "returned_count": len(items),
            "omitted_count": max(0, total - next_offset),
            "output_budget_limited": len(items) < requested_count,
        }
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) <= MODEL_TOOL_OUTPUT_MAX_CHARS:
            return candidate
        items = items[:-1]
    return {
        "found": False,
        "error": "source_structure_item_exceeds_model_read_contract",
        "cursor": cursor,
        "max_chars": MODEL_TOOL_OUTPUT_MAX_CHARS,
        "next_action": "Retry with a later cursor only after reporting this contract error.",
    }


__all__ = ["build_source_structure_page"]
