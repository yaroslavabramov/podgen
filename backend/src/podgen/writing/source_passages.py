"""Exact attributed excerpts, with a separately pinned author-only selection strategy."""

from podgen.domain.errors import ContractError

from .projection_versions import V9, manifest_version

KEY = "author_source_excerpts"
VERSION = "selected-card-excerpts/1"


def validate_strategy(strategy, projection):
    if strategy is not None and (strategy != VERSION or projection != V9):
        raise ContractError("unsupported_author_source_excerpts")
    return strategy


def from_manifest(manifest):
    strategy = validate_strategy(manifest.get(KEY), manifest_version(manifest))
    if strategy is not None and manifest.get("behavior_profile") != "production/1":
        raise ContractError("unsupported_author_source_excerpts")
    return strategy


def card_source_passages(card, documents, *, strategy=None):
    validate_strategy(strategy, V9)
    if strategy == VERSION:
        excerpts = card.excerpts
    else:
        excerpts = []
        seen = set()
        for index in getattr(card, "narrative_excerpt_indices", None) or []:
            excerpt = card.excerpts[index]
            identity = (excerpt.document, excerpt.start, excerpt.end)
            if identity not in seen:
                seen.add(identity)
                excerpts.append(excerpt)
    return [dict(source=documents[excerpt.document].title, text=excerpt.text) for excerpt in excerpts]
