from __future__ import annotations

import uuid
from typing import Any


def provider_filters(
    source_ids: list[str],
    source_version_ids: list[str] | None = None,
) -> dict[str, Any]:
    valid = [source_id for source_id in source_ids if _uuid_or_none(source_id)]
    valid_versions = [version_id for version_id in (source_version_ids or []) if _uuid_or_none(version_id)]
    base_filters = [
        {"type": "eq", "key": "is_active", "value": True},
        {"type": "eq", "key": "agent_facing", "value": True},
        {"type": "eq", "key": "canonical_retrieval_surface", "value": True},
    ]
    scope_filters: list[dict[str, Any]] = []
    if valid:
        source_filters = [{"type": "eq", "key": "source_id", "value": source_id} for source_id in valid]
        scope_filters.append(
            {"type": "or", "filters": source_filters} if len(source_filters) > 1 else source_filters[0]
        )
    if valid_versions:
        version_filters = [
            {"type": "eq", "key": "source_version_id", "value": version_id}
            for version_id in valid_versions
        ]
        scope_filters.append(
            {"type": "or", "filters": version_filters} if len(version_filters) > 1 else version_filters[0]
        )
    return {
        "type": "and",
        "filters": [*base_filters, *scope_filters],
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
