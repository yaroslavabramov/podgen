from __future__ import annotations

import re
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


def window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str]:
    normalized = text.strip()
    if not normalized:
        return "", locator or "source window", EvidenceLocatorType.UNKNOWN
    page_window = page_window_for_locator(normalized, locator=locator, max_chars=max_chars)
    if page_window:
        return page_window
    timestamp_window = timestamp_window_for_locator(normalized, locator=locator, max_chars=max_chars)
    if timestamp_window:
        return timestamp_window
    heading_window = heading_window_for_locator(normalized, locator=locator, max_chars=max_chars)
    if heading_window:
        return heading_window
    if locator:
        idx = normalized.lower().find(locator.lower())
        if idx >= 0:
            start = max(0, idx - max_chars // 4)
            end = min(len(normalized), start + max_chars)
            return normalized[start:end], f"around phrase: {locator}", EvidenceLocatorType.SECTION
    return normalized[:max_chars], locator or "source beginning", EvidenceLocatorType.SOURCE_LEVEL


def source_outline(text: str, *, max_items: int) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    items: list[dict[str, Any]] = []
    for page in page_markers(text)[:max_items]:
        items.append({"locator": f"page {page['page']}", "locator_type": EvidenceLocatorType.PAGE, "preview": page["line"][:240]})
    if len(items) < max_items:
        for timestamp in timestamp_markers(text)[: max_items - len(items)]:
            items.append(
                {
                    "locator": timestamp["timestamp"],
                    "locator_type": EvidenceLocatorType.TIMESTAMP,
                    "preview": timestamp["line"][:240],
                }
            )
    if len(items) < max_items:
        for heading in heading_markers(text)[: max_items - len(items)]:
            items.append({"locator": heading["heading"], "locator_type": EvidenceLocatorType.SECTION, "preview": heading["line"][:240]})
    return items


def page_window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
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
    snippet = text[start:end].strip()[:max_chars]
    label = f"page {start_page}" if start_page == end_page else f"pages {start_page}-{end_page}"
    return snippet, label, EvidenceLocatorType.PAGE


def timestamp_window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    target_seconds = parse_timestamp(locator)
    if target_seconds is None:
        return None
    markers = timestamp_markers(text)
    if not markers:
        return None
    nearest = min(markers, key=lambda marker: abs(int(marker["seconds"]) - target_seconds))
    start = max(0, int(nearest["start"]) - max_chars // 3)
    end = min(len(text), start + max_chars)
    return text[start:end].strip(), str(nearest["timestamp"]), EvidenceLocatorType.TIMESTAMP


def heading_window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    if not locator.strip():
        return None
    lower = locator.lower()
    for heading in heading_markers(text):
        if lower in str(heading["heading"]).lower():
            start = int(heading["start"])
            end = min(len(text), start + max_chars)
            return text[start:end].strip(), str(heading["heading"]), EvidenceLocatorType.SECTION
    return None


def search_text_windows(text: str, *, query: str, max_windows: int, max_chars: int) -> list[dict[str, Any]]:
    tokens = query_tokens(query)
    if not tokens:
        snippet = text.strip()[:max_chars]
        return [{"snippet_text": snippet, "locator_text": "source beginning", "locator_type": EvidenceLocatorType.SOURCE_LEVEL, "score": 0.0}]
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
            snippet = text[start:end].strip()
            locator = nearest_locator_for_position(text, match.start()) or f"around phrase: {token}"
            score = sum(snippet.lower().count(item) for item in tokens[:8])
            matches.append({"snippet_text": snippet, "locator_text": locator, "locator_type": locator_type_for_label(locator), "score": float(score)})
            if len(matches) >= max_windows:
                return sorted(matches, key=lambda item: item["score"], reverse=True)
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:max_windows]


def nearest_locator_for_position(text: str, position: int) -> str | None:
    candidates: list[tuple[int, str]] = []
    for marker in page_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), f"page {marker['page']}"))
    for marker in timestamp_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["timestamp"])))
    for marker in heading_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["heading"])))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def locator_type_for_label(label: str) -> str:
    if parse_page_locator(label):
        return EvidenceLocatorType.PAGE
    if parse_timestamp(label) is not None:
        return EvidenceLocatorType.TIMESTAMP
    if label.startswith("around phrase:"):
        return EvidenceLocatorType.SECTION
    return EvidenceLocatorType.SECTION


def select_chunk_window(chunks: list[Chunk], *, locator: str) -> Chunk:
    if locator:
        lower = locator.lower()
        for chunk in chunks:
            heading = " > ".join(chunk.heading_path or [])
            if lower in heading.lower() or lower in chunk.content.lower():
                return chunk
    return chunks[0]



__all__ = [
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
    "timestamp_markers",
    "timestamp_window_for_locator",
    "window_for_locator",
]
