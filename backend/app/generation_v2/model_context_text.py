from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_INTERNAL_SOURCE_MARKER = re.compile(
    r"\[source_id=[0-9a-f-]{36}\s+page_marker=\d+\s+paragraph=\d+\]\s*",
    re.IGNORECASE,
)
_GENERIC_WINDOW_LOCATOR = re.compile(r"source\s+window\s+\d+", re.IGNORECASE)


def model_preview_text(
    value: Any,
    *,
    replacements: Mapping[str, str] | None = None,
    collapse_whitespace: bool = True,
) -> str:
    """Remove storage-only markup from a model-facing text preview.

    The persisted source text is intentionally left untouched. Callers pass
    only known internal identifiers, so UUID-like facts in source material are
    not erased indiscriminately.
    """

    text = _INTERNAL_SOURCE_MARKER.sub("", str(value or ""))
    for internal_id, public_ref in sorted(
        (replacements or {}).items(),
        key=lambda item: len(str(item[0])),
        reverse=True,
    ):
        if internal_id:
            text = text.replace(str(internal_id), str(public_ref))
    if collapse_whitespace:
        return " ".join(text.split())
    return text


def stable_unique_texts(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = model_preview_text(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def is_generic_window_locator(value: Any) -> bool:
    return bool(_GENERIC_WINDOW_LOCATOR.fullmatch(" ".join(str(value or "").split())))


__all__ = ["is_generic_window_locator", "model_preview_text", "stable_unique_texts"]
