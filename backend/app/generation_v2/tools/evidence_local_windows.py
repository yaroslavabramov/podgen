from __future__ import annotations

import re
from typing import Any

from app.generation_v2.tools.evidence_utils import (
    _retrieval_channel_for_version,
    _source_outline,
    _uuid_or_none,
    _version_can_back_local_window,
    _window_for_locator,
)
from app.models.generation_v2 import EvidenceLocatorType, SourceVersion, V2RecordStatus


def _source_version_text(version: SourceVersion) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


async def resolve_local_fallback_source_version(owner: Any, *, source_id: str, source_version_id: str) -> SourceVersion | None:
    selected_version_ids = set(str(item) for item in owner.run_view.selected_source_version_ids)
    selected_source_ids = set(str(item) for item in owner.run_view.selected_source_ids)
    target_source_id = source_id
    if source_version_id and _uuid_or_none(source_version_id):
        import uuid

        version = await owner.db.get(SourceVersion, uuid.UUID(source_version_id))
        if version is None or str(version.id) not in selected_version_ids or str(version.source_id) not in selected_source_ids:
            return None
        if _version_can_back_local_window(version):
            return version
        target_source_id = str(version.source_id)
    if not target_source_id or not _uuid_or_none(target_source_id) or target_source_id not in selected_source_ids:
        return None
    import uuid

    from sqlalchemy import select

    from app.generation_v2.tools.evidence_utils import _readable_version_priority, _uuid_list

    result = await owner.db.execute(
        select(SourceVersion).where(
            SourceVersion.source_id == uuid.UUID(target_source_id),
            SourceVersion.id.in_(_uuid_list(list(selected_version_ids))),
            SourceVersion.status == V2RecordStatus.READY,
        )
    )
    versions = [version for version in result.scalars() if _version_can_back_local_window(version)]
    versions.sort(key=lambda version: _readable_version_priority(version.kind))
    return versions[0] if versions else None


async def read_source_window(
    owner: Any,
    version: SourceVersion,
    *,
    locator: str,
    max_chars: int,
) -> tuple[str, str, str, str]:
    text = _source_version_text(version)
    if text.strip():
        snippet, locator_text, locator_type = _window_for_locator(text, locator=locator, max_chars=max_chars)
        return snippet, locator_text, locator_type, _retrieval_channel_for_version(version)
    return "", "", EvidenceLocatorType.UNKNOWN, _retrieval_channel_for_version(version)


async def source_windows(owner: Any, version: SourceVersion, *, max_windows: int) -> list[dict[str, Any]]:
    text = _source_version_text(version)
    if not text.strip():
        return []
    outline = _source_outline(text, max_items=max_windows)
    if outline:
        windows = []
        for item in outline[:max_windows]:
            snippet, locator_text, locator_type = _window_for_locator(text, locator=item["locator"], max_chars=1600)
            if snippet.strip():
                windows.append(
                    {
                        "locator_text": locator_text,
                        "locator_type": locator_type,
                        "snippet_text": snippet,
                        "channel": _retrieval_channel_for_version(version),
                    }
                )
        if windows:
            return windows
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
    windows = []
    for idx, paragraph in enumerate(paragraphs[:max_windows]):
        windows.append(
            {
                "locator_text": f"source window {idx + 1}",
                "locator_type": EvidenceLocatorType.SOURCE_LEVEL,
                "snippet_text": paragraph[:1600],
                "channel": _retrieval_channel_for_version(version),
            }
        )
    return windows


__all__ = [
    "_source_version_text",
    "read_source_window",
    "resolve_local_fallback_source_version",
    "source_windows",
]
