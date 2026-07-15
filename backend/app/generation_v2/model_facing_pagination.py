from __future__ import annotations

import json
from typing import Any

from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS


def fit_model_page(
    payload: dict[str, Any],
    *,
    requested_limit: int,
    items_key: str = "items",
    error_code: str = "workspace_item_exceeds_model_read_contract",
) -> dict[str, Any]:
    """Fit whole model-facing records into one tool result without truncation."""

    items = list(payload.get(items_key) or [])
    page = dict(payload.get("page") or {})
    cursor = _non_negative_int(page.get("cursor"))
    total = max(cursor + len(items), _non_negative_int(page.get("total_count")))
    safe_limit = max(1, int(requested_limit))
    available_count = min(safe_limit, max(0, total - cursor))

    for returned in range(len(items), -1, -1):
        if returned == 0 and items:
            break
        candidate = dict(payload)
        candidate[items_key] = items[:returned]
        candidate["page"] = {
            **page,
            "total_count": total,
            "returned_count": returned,
            "omitted_count": max(0, total - (cursor + returned)),
            "cursor": str(cursor),
            "next_cursor": str(cursor + returned) if cursor + returned < total else "",
            "requested_limit": safe_limit,
            "output_budget_limited": returned < available_count,
        }
        if _model_output_chars(candidate) <= MODEL_TOOL_OUTPUT_MAX_CHARS:
            return candidate

    section = str(payload.get("section") or "workspace")
    first_item_payload = dict(payload)
    first_item_payload[items_key] = items[:1]
    first_item_payload["page"] = {
        **page,
        "total_count": total,
        "returned_count": min(1, len(items)),
        "cursor": str(cursor),
    }
    return {
        "accepted": False,
        "error": error_code,
        "section": section,
        "cursor": str(cursor),
        "max_chars": MODEL_TOOL_OUTPUT_MAX_CHARS,
        "item_output_chars": _model_output_chars(first_item_payload),
    }


def _model_output_chars(payload: dict[str, Any]) -> int:
    envelope = payload if "accepted" in payload else {"accepted": True, **payload}
    return len(json.dumps(envelope, ensure_ascii=False, default=str))


def _non_negative_int(value: Any) -> int:
    text = str(value or "0")
    return max(0, int(text) if text.isdigit() else 0)


__all__ = ["fit_model_page"]
