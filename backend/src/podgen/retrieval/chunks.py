"""Token-bounded overlapping original ranges; no rewritten, normalized or translated evidence."""

from dataclasses import dataclass

from . import RetrievalUnavailable


@dataclass(frozen=True)
class Chunk:
    source_id: str
    start: int
    end: int


def spans(text, offsets, size, overlap):
    cursor = 0
    while cursor < len(offsets):
        stop = min(cursor + size, len(offsets))
        start, end = offsets[cursor][0], offsets[stop - 1][1]
        if not 0 <= start < end <= len(text):
            raise RetrievalUnavailable("source_search_invalid_token_offsets")
        yield start, end
        if stop == len(offsets):
            break
        cursor = stop - overlap
