"""Compact model-facing locators; a preview is never a source read receipt."""

import json
import re

from podgen.persistence.source_reading import read_sources

from . import RetrievalUnavailable
from .lexical import words


def excerpt(doc, start, end, query, width):
    original_start = start
    content = doc.text[start:end]
    for word in sorted(set(words(query)), key=lambda value: (-len(value), value)):
        pattern = r"(?:-\s*\n\s*)?".join(map(re.escape, word))
        match = re.search(pattern, content, re.I)
        if match:
            start += max(0, match.start() - width // 3)
            break
    stop = min(end, start + width)
    return dict(
        preview=content[start - original_start : stop - original_start], preview_start=start, preview_end=stop
    )


def page(rows, documents, query, offset, profile):
    selected = rows[offset : offset + profile["page_hits"]]
    with read_sources([documents[row["source_id"]] for row in selected]):
        return _page(rows, documents, query, offset, profile)


def _page(rows, documents, query, offset, profile):
    result = dict(hits=[], sources={}, next_offset=None, total_hits=len(rows))
    for row in rows[offset : offset + profile["page_hits"]]:
        key = row["source_id"]
        doc = documents[key]
        hit = dict(
            source_id=key,
            start=row["start"],
            end=row["end"],
            read_start=row["read_start"],
            **excerpt(doc, row["start"], row["end"], query, profile["preview_characters"]),
        )
        previous_sources = dict(result["sources"])
        result["sources"][key] = dict(title_preview=doc.title[:96])
        result["hits"].append(hit)
        following = offset + len(result["hits"])
        result["next_offset"] = following if following < len(rows) else None
        if (
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            > profile["max_response_bytes"]
        ):
            result["hits"].pop()
            result["sources"] = previous_sources
            result["next_offset"] = offset + len(result["hits"])
            if not result["hits"]:
                raise RetrievalUnavailable("source_search_preview_budget_exceeded")
            break
    return result
