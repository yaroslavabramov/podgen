from __future__ import annotations

from typing import Any

from app.generation_v2.tools.evidence_text_windows import (
    locator_type_for_label,
    nearest_locator_for_position,
    representative_text_windows,
    resolve_window_for_locator,
    source_outline,
)
from app.generation_v2.tools.evidence_utils import (
    _retrieval_channel_for_version,
    _uuid_or_none,
    _version_can_back_local_window,
)
from app.generation_v2.tools.evidence_window_refs import create_window_ref, is_window_ref, resolve_window_ref
from app.models.generation_v2 import (
    EvidenceLocatorType,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)
from app.models.source import Source, SourceType


def _source_version_text(version: SourceVersion) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


async def resolve_local_fallback_source_version(
    owner: Any, *, source_id: str, source_version_id: str
) -> SourceVersion | None:
    selected_version_ids = set(str(item) for item in owner.run_view.selected_source_version_ids)
    selected_source_ids = set(str(item) for item in owner.run_view.selected_source_ids)
    target_source_id = source_id
    if source_version_id and _uuid_or_none(source_version_id):
        import uuid

        version = await owner.db.get(SourceVersion, uuid.UUID(source_version_id))
        if (
            version is None
            or str(version.id) not in selected_version_ids
            or str(version.source_id) not in selected_source_ids
        ):
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
        allow_timestamps = await source_allows_timestamp_locators(owner, version)
        if is_window_ref(locator):
            try:
                window = resolve_window_ref(locator, source_version_id=version.id, text=text)
            except ValueError:
                return "", locator, EvidenceLocatorType.UNKNOWN, _retrieval_channel_for_version(version)
            locator_text = (
                nearest_locator_for_position(
                    text,
                    window.start_offset,
                    allow_timestamps=allow_timestamps,
                )
                or "source window"
            )
            locator_type = locator_type_for_label(locator_text, allow_timestamps=allow_timestamps)
            if locator_text == "source window":
                locator_type = EvidenceLocatorType.SOURCE_LEVEL
            return (
                text[window.start_offset : window.end_offset],
                locator_text,
                locator_type,
                _retrieval_channel_for_version(version),
            )
        resolved = resolve_window_for_locator(
            text,
            locator=locator,
            max_chars=max_chars,
            allow_timestamps=allow_timestamps,
        )
        if resolved is not None:
            return (
                resolved.snippet_text,
                resolved.locator_text,
                resolved.locator_type,
                _retrieval_channel_for_version(version),
            )
    return "", "", EvidenceLocatorType.UNKNOWN, _retrieval_channel_for_version(version)


async def source_windows(owner: Any, version: SourceVersion, *, max_windows: int) -> list[dict[str, Any]]:
    text = _source_version_text(version)
    if not text.strip():
        return []
    allow_timestamps = await source_allows_timestamp_locators(owner, version)
    outline = source_outline(text, max_items=max_windows, allow_timestamps=allow_timestamps)
    if outline:
        windows = []
        for item in outline[:max_windows]:
            resolved = resolve_window_for_locator(
                text,
                locator=item["locator"],
                max_chars=1600,
                allow_timestamps=allow_timestamps,
            )
            if resolved is not None:
                windows.append(
                    {
                        "window_ref": create_window_ref(
                            source_version_id=version.id,
                            text=text,
                            start_offset=resolved.start_offset,
                            end_offset=resolved.end_offset,
                        ),
                        "locator_text": resolved.locator_text,
                        "locator_type": resolved.locator_type,
                        "snippet_text": resolved.snippet_text,
                        "channel": _retrieval_channel_for_version(version),
                    }
                )
        if windows:
            return windows
    windows: list[dict[str, Any]] = []
    for resolved in representative_text_windows(text, max_windows=max_windows, max_chars=1600):
        windows.append(
            {
                "window_ref": create_window_ref(
                    source_version_id=version.id,
                    text=text,
                    start_offset=resolved.start_offset,
                    end_offset=resolved.end_offset,
                ),
                "locator_text": resolved.locator_text,
                "locator_type": resolved.locator_type,
                "snippet_text": resolved.snippet_text,
                "channel": _retrieval_channel_for_version(version),
            }
        )
    return windows


async def source_allows_timestamp_locators(owner: Any, version: SourceVersion) -> bool:
    if version.kind == SourceRepresentationKind.TRANSCRIPT:
        return True
    metadata = version.metadata_ or {}
    declared_type = str(
        metadata.get("source_type") or metadata.get("media_type") or metadata.get("asset_type") or ""
    ).lower()
    if declared_type in {SourceType.AUDIO, SourceType.VIDEO, "audio", "video", "transcript"}:
        return True
    try:
        source = await owner.db.get(Source, version.source_id)
    except (AttributeError, TypeError):
        source = None
    source_type = str(getattr(source, "source_type", "") or "").lower()
    return source_type in {SourceType.AUDIO, SourceType.VIDEO, "audio", "video"}


__all__ = [
    "_source_version_text",
    "read_source_window",
    "resolve_local_fallback_source_version",
    "source_allows_timestamp_locators",
    "source_windows",
]
