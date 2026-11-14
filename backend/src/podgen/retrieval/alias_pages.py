"""Bounded model-facing alias pages over complete immutable grouped selectors."""

import json

from podgen.persistence.source_reading import read_sources

from . import QueryRejected, RetrievalUnavailable

INLINE = 2


def attach(hit, row, *, query, within, position, result_version=None):
    aliases = row["aliases"]
    hit.update(aliases=aliases[:INLINE], alias_count=len(aliases), support_relation=row["support_relation"])
    if row["support_relation"] in ("shared_original_text", "overlapping_text_variants"):
        hit["independent_confirmation"] = False
    if row.get("variants"):
        hit["variants"] = row["variants"]
    if len(aliases) > INLINE:
        hit["next_alias_page"] = dict(
            tool="search_sources",
            arguments=dict(query=query, within=within, offset=INLINE, aliases_of=position),
        )
        if result_version is not None:
            hit["next_alias_page"]["arguments"]["result_version"] = result_version


def page(rows, documents, *, query, within, position, offset, profile, metadata=None):
    if not 0 <= position < len(rows):
        raise QueryRejected("unknown_search_passage_group")
    aliases = rows[position]["aliases"]
    selected = aliases[offset : offset + profile["page_hits"]]
    result = dict(
        retrieval_policy=profile["retrieval_policy"],
        aliases_of=position,
        aliases=[],
        alias_count=len(aliases),
        sources={},
        next_offset=None,
    )
    result.update(metadata or {})
    with read_sources(
        [documents[source_id] for source_id in sorted({alias["source_id"] for alias in selected})]
    ):
        for alias in selected:
            prior = dict(result["sources"])
            result["aliases"].append(alias)
            result["sources"][alias["source_id"]] = dict(
                title_preview=documents[alias["source_id"]].title[:96]
            )
            following = offset + len(result["aliases"])
            result["next_offset"] = following if following < len(aliases) else None
            if (
                len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
                > profile["max_response_bytes"]
            ):
                result["aliases"].pop()
                result["sources"] = prior
                result["next_offset"] = offset + len(result["aliases"])
                if not result["aliases"]:
                    raise RetrievalUnavailable("source_search_preview_budget_exceeded")
                break
    return result
