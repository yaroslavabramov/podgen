from __future__ import annotations

import re
import uuid
from typing import Any

from app.generation_v2.tools.evidence_text_windows import (
    locator_type_for_label,
    nearest_locator_for_position,
)
from app.generation_v2.tools.evidence_window_refs import (
    WindowRefError,
    create_window_ref,
    resolve_window_ref,
)
from app.models.generation_v2 import EvidenceLocatorType


SOURCE_PASSAGE_MAX_CHARS = 3_000


def source_passages_for_range(
    *,
    source_version_id: uuid.UUID,
    text: str,
    start_offset: int,
    end_offset: int,
    allow_timestamps: bool,
    max_chars: int = SOURCE_PASSAGE_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Split one delivered range into stable, lossless source passages.

    Passage refs are signed source-window refs. Every source character in the
    delivered range belongs to exactly one passage; model-facing text is never
    shortened or reconstructed from previews.
    """

    if start_offset < 0 or end_offset <= start_offset or end_offset > len(text):
        raise ValueError("invalid_source_passage_range")
    max_chars = max(200, min(int(max_chars), SOURCE_PASSAGE_MAX_CHARS))
    ranges = _lossless_passage_ranges(text, start_offset, end_offset, max_chars=max_chars)
    passages: list[dict[str, Any]] = []
    for passage_start, passage_end in ranges:
        locator = nearest_locator_for_position(
            text,
            passage_start,
            allow_timestamps=allow_timestamps,
        )
        passages.append(
            {
                "passage_ref": create_window_ref(
                    source_version_id=source_version_id,
                    text=text,
                    start_offset=passage_start,
                    end_offset=passage_end,
                ),
                "text": text[passage_start:passage_end],
                "range": {
                    "start_offset": passage_start,
                    "end_offset": passage_end,
                    "offset_unit": "unicode_codepoint",
                    "end_exclusive": True,
                },
                "locator": locator or "source passage",
                "locator_type": (
                    locator_type_for_label(locator, allow_timestamps=allow_timestamps)
                    if locator
                    else EvidenceLocatorType.SOURCE_LEVEL
                ),
            }
        )
    return passages


def resolve_source_passage_ref(
    passage_ref: str,
    *,
    source_version_id: uuid.UUID,
    text: str,
) -> dict[str, int]:
    try:
        passage = resolve_window_ref(
            passage_ref,
            source_version_id=source_version_id,
            text=text,
        )
    except (ValueError, WindowRefError) as exc:
        raise ValueError("invalid_or_stale_passage_ref") from exc
    if passage.end_offset - passage.start_offset > SOURCE_PASSAGE_MAX_CHARS:
        raise ValueError("passage_ref_exceeds_exact_evidence_contract")
    return {
        "start_offset": passage.start_offset,
        "end_offset": passage.end_offset,
    }


def _lossless_passage_ranges(
    text: str,
    start_offset: int,
    end_offset: int,
    *,
    max_chars: int,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    position = start_offset
    while position < end_offset:
        hard_end = min(end_offset, position + max_chars)
        passage_end = hard_end
        if hard_end < end_offset:
            search_start = max(position + 1, hard_end - min(800, max_chars // 3))
            tail = text[search_start:hard_end]
            boundaries: list[int] = []
            for pattern in (r"\n[ \t]*\n+", r"(?<=[.!?])(?:[ \t]+|\n+)", r"\s+"):
                boundaries.extend(search_start + match.end() for match in re.finditer(pattern, tail))
                if boundaries:
                    break
            if boundaries:
                passage_end = max(boundaries)
        if passage_end <= position:
            passage_end = hard_end
        ranges.append((position, passage_end))
        position = passage_end
    return ranges


__all__ = [
    "SOURCE_PASSAGE_MAX_CHARS",
    "resolve_source_passage_ref",
    "source_passages_for_range",
]
