from __future__ import annotations

import uuid
from typing import Any


def provider_filters(source_ids: list[str]) -> dict[str, Any]:
    valid = [source_id for source_id in source_ids if _uuid_or_none(source_id)]
    base_filters = [
        {"type": "eq", "key": "is_active", "value": True},
        {"type": "eq", "key": "agent_facing", "value": True},
        {"type": "eq", "key": "canonical_retrieval_surface", "value": True},
    ]
    if not valid:
        return {"type": "and", "filters": base_filters}
    source_filters = [{"type": "eq", "key": "source_id", "value": source_id} for source_id in valid]
    return {
        "type": "and",
        "filters": [
            *base_filters,
            {"type": "or", "filters": source_filters} if len(source_filters) > 1 else source_filters[0],
        ],
    }


def provider_snippet(item: Any) -> str:
    content = getattr(item, "content", []) or []
    return "\n\n".join(str(_part_text(part) or "") for part in content if _part_text(part)).strip()


def _part_text(part: Any) -> str | None:
    if isinstance(part, dict):
        return part.get("text")
    return getattr(part, "text", None)


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["provider_filters", "provider_snippet"]
