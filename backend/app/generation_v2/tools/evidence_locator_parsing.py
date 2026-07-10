from __future__ import annotations

import re
from typing import Any


def page_markers(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?im)^\s*(?:-{2,}\s*)?(?:\[?\s*(?:page|p\.)\s+(\d+)\s*\]?)(?:\s*-{2,})?\s*$")
    return [
        {
            "page": int(match.group(1)),
            "start": match.start(),
            "line": match.group(0).strip(),
        }
        for match in pattern.finditer(text)
    ]


def heading_markers(text: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        is_heading = bool(re.match(r"^#{1,6}\s+\S", stripped)) or (
            4 <= len(stripped) <= 120
            and not stripped.endswith(".")
            and stripped[:1].isupper()
            and len(stripped.split()) <= 14
        )
        if is_heading:
            markers.append({"heading": stripped.lstrip("# ").strip(), "start": offset, "line": stripped})
        offset += len(line)
    return markers


def timestamp_markers(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?m)(?:^|\s|\[)(\d{1,2}:\d{2}(?::\d{2})?)(?:\]|\s|$)")
    markers = []
    for match in pattern.finditer(text):
        seconds = parse_timestamp(match.group(1))
        if seconds is None:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        markers.append({"timestamp": match.group(1), "seconds": seconds, "start": match.start(), "line": text[line_start:line_end].strip()})
    return markers


def parse_page_locator(locator: str) -> tuple[int, int] | None:
    match = re.search(r"\bpages?\s+(\d+)(?:\s*[-–]\s*(\d+))?\b|\bp\.\s*(\d+)\b", locator or "", flags=re.IGNORECASE)
    if not match:
        return None
    start = int(match.group(1) or match.group(3))
    end = int(match.group(2) or start)
    return min(start, end), max(start, end)


def parse_timestamp(value: str) -> int | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", value or "")
    if not match:
        return None
    first, second, third = match.groups()
    if third is None:
        return int(first) * 60 + int(second)
    return int(first) * 3600 + int(second) * 60 + int(third)


def query_tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[^\W_]{3,}", (text or "").lower(), flags=re.UNICODE) if token]


__all__ = [
    "heading_markers",
    "page_markers",
    "parse_page_locator",
    "parse_timestamp",
    "query_tokens",
    "timestamp_markers",
]
