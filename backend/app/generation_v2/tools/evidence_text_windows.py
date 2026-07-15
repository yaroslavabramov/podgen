from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil
from typing import Any

from app.generation_v2.tools.evidence_locator_parsing import (
    heading_markers,
    page_markers,
    parse_page_locator,
    parse_timestamp,
    query_tokens,
    timestamp_markers,
)
from app.models.chunk import Chunk
from app.models.generation_v2 import EvidenceLocatorType


@dataclass(frozen=True)
class ResolvedTextWindow:
    snippet_text: str
    locator_text: str
    locator_type: str
    start_offset: int
    end_offset: int


def window_for_locator(
    text: str,
    *,
    locator: str,
    max_chars: int,
    allow_timestamps: bool = False,
) -> tuple[str, str, str]:
    resolved = resolve_window_for_locator(
        text,
        locator=locator,
        max_chars=max_chars,
        allow_timestamps=allow_timestamps,
    )
    if resolved is None:
        return "", locator or "source window", EvidenceLocatorType.UNKNOWN
    return resolved.snippet_text, resolved.locator_text, resolved.locator_type


def resolve_window_for_locator(
    text: str,
    *,
    locator: str,
    max_chars: int,
    allow_timestamps: bool = False,
) -> ResolvedTextWindow | None:
    if not text.strip():
        return None
    requested = str(locator or "").strip()
    if not requested or requested.lower() in {"beginning", "source beginning", "start", "source start"}:
        return _resolved_range(
            text,
            start=0,
            end=min(len(text), max_chars),
            locator_text="source beginning",
            locator_type=EvidenceLocatorType.SOURCE_LEVEL,
        )
    page_window = resolve_page_window(text, locator=requested, max_chars=max_chars)
    if page_window:
        return page_window
    if allow_timestamps:
        timestamp_window = resolve_timestamp_window(text, locator=requested, max_chars=max_chars)
        if timestamp_window:
            return timestamp_window
    elif _looks_like_timestamp_locator(requested):
        return None
    heading_window = resolve_heading_window(text, locator=requested, max_chars=max_chars)
    if heading_window:
        return heading_window
    idx = text.casefold().find(requested.casefold())
    if idx < 0:
        return None
    start = max(0, idx - max_chars // 4)
    end = min(len(text), start + max_chars)
    return _resolved_range(
        text,
        start=start,
        end=end,
        locator_text=f"around phrase: {requested}",
        locator_type=EvidenceLocatorType.SECTION,
    )


def source_outline(text: str, *, max_items: int, allow_timestamps: bool = False) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    pages = page_markers(text)
    if pages:
        return [
            {
                "locator": f"page {page['page']}",
                "locator_type": EvidenceLocatorType.PAGE,
                "preview": page["line"][:240],
            }
            for page in _representative_items(pages, max_items=max_items)
        ]
    timestamps = timestamp_markers(text) if allow_timestamps else []
    if timestamps:
        return [
            {
                "locator": timestamp["timestamp"],
                "locator_type": EvidenceLocatorType.TIMESTAMP,
                "preview": timestamp["line"][:240],
            }
            for timestamp in _representative_items(timestamps, max_items=max_items)
        ]
    headings = heading_markers(text)
    return [
        {
            "locator": heading["heading"],
            "locator_type": EvidenceLocatorType.SECTION,
            "preview": heading["line"][:240],
        }
        for heading in _representative_items(headings, max_items=max_items)
    ]


def page_window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    resolved = resolve_page_window(text, locator=locator, max_chars=max_chars)
    if resolved is None:
        return None
    return resolved.snippet_text, resolved.locator_text, resolved.locator_type


def resolve_page_window(text: str, *, locator: str, max_chars: int) -> ResolvedTextWindow | None:
    page_range = parse_page_locator(locator)
    if not page_range:
        return None
    start_page, end_page = page_range
    markers = page_markers(text)
    if not markers:
        return None
    start_marker = next((marker for marker in markers if marker["page"] == start_page), None)
    if start_marker is None:
        return None
    end_marker = next((marker for marker in markers if marker["page"] > end_page), None)
    start = int(start_marker["start"])
    end = int(end_marker["start"]) if end_marker else len(text)
    end = min(end, start + max_chars)
    label = f"page {start_page}" if start_page == end_page else f"pages {start_page}-{end_page}"
    return _resolved_range(
        text,
        start=start,
        end=end,
        locator_text=label,
        locator_type=EvidenceLocatorType.PAGE,
    )


def timestamp_window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    resolved = resolve_timestamp_window(text, locator=locator, max_chars=max_chars)
    if resolved is None:
        return None
    return resolved.snippet_text, resolved.locator_text, resolved.locator_type


def resolve_timestamp_window(text: str, *, locator: str, max_chars: int) -> ResolvedTextWindow | None:
    target_seconds = parse_timestamp(locator)
    if target_seconds is None:
        return None
    markers = timestamp_markers(text)
    if not markers:
        return None
    selected = next((marker for marker in markers if int(marker["seconds"]) == target_seconds), None)
    if selected is None:
        return None
    start = int(selected["start"])
    next_marker = next((marker for marker in markers if int(marker["start"]) > start), None)
    natural_end = int(next_marker["start"]) if next_marker else len(text)
    end = min(natural_end, start + max_chars)
    return _resolved_range(
        text,
        start=start,
        end=end,
        locator_text=str(selected["timestamp"]),
        locator_type=EvidenceLocatorType.TIMESTAMP,
    )


def heading_window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    resolved = resolve_heading_window(text, locator=locator, max_chars=max_chars)
    if resolved is None:
        return None
    return resolved.snippet_text, resolved.locator_text, resolved.locator_type


def resolve_heading_window(text: str, *, locator: str, max_chars: int) -> ResolvedTextWindow | None:
    if not locator.strip():
        return None
    lower = locator.lower()
    for heading in heading_markers(text):
        if lower in str(heading["heading"]).lower():
            start = int(heading["start"])
            end = min(len(text), start + max_chars)
            return _resolved_range(
                text,
                start=start,
                end=end,
                locator_text=str(heading["heading"]),
                locator_type=EvidenceLocatorType.SECTION,
            )
    return None


def search_text_windows(
    text: str,
    *,
    query: str,
    max_windows: int,
    max_chars: int,
    allow_timestamps: bool = False,
) -> list[dict[str, Any]]:
    tokens = query_tokens(query)
    if not tokens:
        resolved = resolve_window_for_locator(text, locator="", max_chars=max_chars)
        if resolved is None:
            return []
        return [
            {
                "snippet_text": resolved.snippet_text,
                "locator_text": resolved.locator_text,
                "locator_type": resolved.locator_type,
                "score": 0.0,
                "start_offset": resolved.start_offset,
                "end_offset": resolved.end_offset,
            }
        ]
    matches: list[dict[str, Any]] = []
    lower = text.lower()
    seen_starts: set[int] = set()
    for token in tokens[:8]:
        for match in re.finditer(re.escape(token), lower):
            start = max(0, match.start() - max_chars // 4)
            if any(abs(start - seen) < max_chars // 3 for seen in seen_starts):
                continue
            seen_starts.add(start)
            end = min(len(text), start + max_chars)
            resolved = _resolved_range(
                text,
                start=start,
                end=end,
                locator_text=(
                    nearest_locator_for_position(text, match.start(), allow_timestamps=allow_timestamps)
                    or f"around phrase: {token}"
                ),
                locator_type=EvidenceLocatorType.SECTION,
            )
            if resolved is None:
                continue
            snippet = resolved.snippet_text
            locator = resolved.locator_text
            score = sum(snippet.lower().count(item) for item in tokens[:8])
            matches.append(
                {
                    "snippet_text": snippet,
                    "locator_text": locator,
                    "locator_type": locator_type_for_label(locator, allow_timestamps=allow_timestamps),
                    "score": float(score),
                    "start_offset": resolved.start_offset,
                    "end_offset": resolved.end_offset,
                }
            )
            if len(matches) >= max_windows:
                return sorted(matches, key=lambda item: item["score"], reverse=True)
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:max_windows]


def nearest_locator_for_position(text: str, position: int, *, allow_timestamps: bool = False) -> str | None:
    candidates: list[tuple[int, str]] = []
    for marker in page_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), f"page {marker['page']}"))
    if allow_timestamps:
        for marker in timestamp_markers(text):
            if int(marker["start"]) <= position:
                candidates.append((int(marker["start"]), str(marker["timestamp"])))
    for marker in heading_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["heading"])))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def locator_type_for_label(label: str, *, allow_timestamps: bool = True) -> str:
    if parse_page_locator(label):
        return EvidenceLocatorType.PAGE
    if allow_timestamps and parse_timestamp(label) is not None:
        return EvidenceLocatorType.TIMESTAMP
    if label.startswith("around phrase:"):
        return EvidenceLocatorType.SECTION
    return EvidenceLocatorType.SECTION


def representative_text_windows(text: str, *, max_windows: int, max_chars: int) -> list[ResolvedTextWindow]:
    if not text.strip() or max_windows < 1:
        return []
    window_count = min(max_windows, max(1, ceil(len(text) / max_chars)))
    last_start = max(0, len(text) - max_chars)
    if window_count == 1:
        target_starts = [0]
    else:
        target_starts = [round(idx * last_start / (window_count - 1)) for idx in range(window_count)]
    paragraph_starts = [match.start() for match in re.finditer(r"(?m)(?:\A|\n\s*\n)\s*(?=\S)", text)]
    selected_starts: list[int] = []
    for target in target_starts:
        nearby_paragraph = min(paragraph_starts, key=lambda start: abs(start - target), default=target)
        start = (
            nearby_paragraph if abs(nearby_paragraph - target) <= max_chars // 3 else _align_window_start(text, target)
        )
        if start not in selected_starts:
            selected_starts.append(start)
    windows: list[ResolvedTextWindow] = []
    for idx, start in enumerate(selected_starts, start=1):
        end = min(len(text), start + max_chars)
        resolved = _resolved_range(
            text,
            start=start,
            end=end,
            locator_text=f"source window {idx}",
            locator_type=EvidenceLocatorType.SOURCE_LEVEL,
        )
        if resolved is not None:
            windows.append(resolved)
    return windows


def _align_window_start(text: str, target: int) -> int:
    if target <= 0:
        return 0
    next_boundary = re.search(r"\s+", text[target : min(len(text), target + 120)])
    if next_boundary is None:
        return target
    return min(len(text) - 1, target + next_boundary.end())


def _looks_like_timestamp_locator(value: str) -> bool:
    return bool(re.fullmatch(r"\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*", value or ""))


def _resolved_range(
    text: str,
    *,
    start: int,
    end: int,
    locator_text: str,
    locator_type: str,
) -> ResolvedTextWindow | None:
    start = max(0, start)
    end = min(len(text), end)
    if end <= start:
        return None
    raw = text[start:end]
    left_trim = len(raw) - len(raw.lstrip())
    right_trim = len(raw) - len(raw.rstrip())
    start += left_trim
    end -= right_trim
    if end <= start:
        return None
    return ResolvedTextWindow(
        snippet_text=text[start:end],
        locator_text=locator_text,
        locator_type=str(locator_type),
        start_offset=start,
        end_offset=end,
    )


def _representative_items(items: list[Any], *, max_items: int) -> list[Any]:
    if max_items < 1 or not items:
        return []
    if len(items) <= max_items:
        return list(items)
    if max_items == 1:
        return [items[0]]
    indexes = [round(idx * (len(items) - 1) / (max_items - 1)) for idx in range(max_items)]
    return [items[idx] for idx in dict.fromkeys(indexes)]


def select_chunk_window(chunks: list[Chunk], *, locator: str) -> Chunk:
    if locator:
        lower = locator.lower()
        for chunk in chunks:
            heading = " > ".join(chunk.heading_path or [])
            if lower in heading.lower() or lower in chunk.content.lower():
                return chunk
    return chunks[0]


__all__ = [
    "ResolvedTextWindow",
    "heading_markers",
    "heading_window_for_locator",
    "locator_type_for_label",
    "nearest_locator_for_position",
    "page_markers",
    "page_window_for_locator",
    "parse_page_locator",
    "parse_timestamp",
    "query_tokens",
    "search_text_windows",
    "select_chunk_window",
    "source_outline",
    "representative_text_windows",
    "resolve_heading_window",
    "resolve_page_window",
    "resolve_timestamp_window",
    "resolve_window_for_locator",
    "timestamp_markers",
    "timestamp_window_for_locator",
    "window_for_locator",
]
