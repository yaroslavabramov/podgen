"""Compact model-facing locators; a preview is never a source read receipt."""

import json
import re

from podgen.persistence.source_reading import read_sources

from . import RetrievalUnavailable
from .config import diverse
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


def page(rows, documents, query, offset, profile, *, aliases_of=None, within=None, metadata=None):
    if diverse(profile) and aliases_of is not None:
        from .alias_pages import page as alias_page

        return alias_page(
            rows,
            documents,
            query=query,
            within=within,
            position=aliases_of,
            offset=offset,
            profile=profile,
            metadata=metadata,
        )
    selected = rows[offset : offset + profile["page_hits"]]
    source_ids = {row["source_id"] for row in selected}
    if diverse(profile):
        from .alias_pages import INLINE

        source_ids.update(alias["source_id"] for row in selected for alias in row.get("aliases", ())[:INLINE])
        source_ids.update(
            version["source_id"]
            for row in selected
            for variant in row.get("variants", ())
            for version in variant["versions"]
        )
    with read_sources([documents[source_id] for source_id in sorted(source_ids)]):
        return _page(rows, documents, query, offset, profile, within=within, metadata=metadata)


def _page(rows, documents, query, offset, profile, *, within=None, metadata=None):
    result = dict(hits=[], sources={}, next_offset=None, total_hits=len(rows))
    result.update(metadata or {})
    if diverse(profile):
        result["retrieval_policy"] = profile["retrieval_policy"]
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
        if diverse(profile) and "aliases" in row:
            from .alias_pages import attach

            attach(
                hit,
                row,
                query=query,
                within=within,
                position=offset + len(result["hits"]),
                result_version=(metadata or {}).get("result_version"),
            )
            for alias in hit["aliases"]:
                result["sources"][alias["source_id"]] = dict(
                    title_preview=documents[alias["source_id"]].title[:96]
                )
            for variant in hit.get("variants", ()):
                for version in variant["versions"]:
                    result["sources"][version["source_id"]] = dict(
                        title_preview=documents[version["source_id"]].title[:96]
                    )
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
