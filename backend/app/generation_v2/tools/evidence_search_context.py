from __future__ import annotations

import uuid

from app.generation_v2.tools.evidence_window_refs import create_window_ref


def expanded_search_context(
    *,
    source_version_id: uuid.UUID,
    text: str,
    match_start: int,
    match_end: int,
    max_chars: int,
) -> dict[str, object]:
    """Build a stable context window that contains a smaller search match."""

    if match_start < 0 or match_end <= match_start or match_end > len(text):
        raise ValueError("invalid_search_match_range")
    limit = max(match_end - match_start, int(max_chars))
    limit = min(limit, len(text))
    remaining = limit - (match_end - match_start)
    left = min(match_start, remaining // 2)
    right = min(len(text) - match_end, remaining - left)
    unused = remaining - left - right
    if unused:
        extra_left = min(match_start - left, unused)
        left += extra_left
        unused -= extra_left
        right += min(len(text) - match_end - right, unused)

    start = match_start - left
    end = match_end + right
    return {
        "start_offset": start,
        "end_offset": end,
        "window_ref": create_window_ref(
            source_version_id=source_version_id,
            text=text,
            start_offset=start,
            end_offset=end,
        ),
    }


__all__ = ["expanded_search_context"]
