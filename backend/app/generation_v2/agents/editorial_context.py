from __future__ import annotations

from app.generation_v2.agents.editorial_evidence_inventory import (
    evidence_inventory,
    evidence_metadata_flags,
    trim,
)
from app.generation_v2.schemas import QuoteCard, SourceCard, SourceMap, SourceTriageReport
from app.generation_v2.source_triage import quote_ref
from app.generation_v2.tools.evidence_refs import SourceRefContext, build_source_ref_context, source_ref_for_id


def source_ref_context(source_cards: list[SourceCard]) -> SourceRefContext:
    return build_source_ref_context(SourceMap(source_cards=source_cards))


def source_inventory(
    source_cards: list[SourceCard],
    *,
    limit: int = 80,
    ref_context: SourceRefContext | None = None,
) -> list[dict]:
    context = ref_context or source_ref_context(source_cards)
    return [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            "title": card.title,
            "source_type": card.source_type,
            "domain": card.domain,
            "published_at": card.published_at,
            "length_hint": card.length_hint,
            "coverage_status": card.coverage_status,
            "version_count": len(card.source_version_ids or card.representation_ids or []),
            "ready_version_count": card.ready_representation_count,
            "failed_version_count": card.failed_representation_count,
            "representation_summary_preview": trim(card.representation_summary, 180),
            "representation_summary_chars": len(card.representation_summary),
            "representation_summary_truncated": len(card.representation_summary) > 180,
            "useful_for": card.useful_for[:8],
            "evidence_ids": card.evidence_ids[:8],
            "brief_hint": trim(card.summary, 420),
            "cautions": card.gaps_or_cautions[:6],
        }
        for card in source_cards[:limit]
    ]


def quote_inventory(
    quote_cards: list[QuoteCard],
    *,
    limit: int = 80,
    ref_context: SourceRefContext | None = None,
) -> list[dict]:
    return [
        {
            "quote_ref": quote_ref(card),
            "source_ref": source_ref_for_id(card.source_id, ref_context) if ref_context else None,
            "evidence_id": card.evidence_id,
            "locator": card.locator,
            "narrative_use": card.narrative_use,
            "confidence": card.confidence,
            "source_kind": card.source_kind,
            "quote_excerpt": trim(card.quote, 500),
        }
        for card in quote_cards[:limit]
    ]


def source_refs(source_ids: list[str], context: SourceRefContext | None) -> list[str]:
    if context is None:
        return ["S??" for source_id in source_ids if source_id]
    return [source_ref_for_id(str(source_id), context) for source_id in source_ids if source_id]


def triage_inventory(
    source_triage: SourceTriageReport | None,
    *,
    source_limit: int = 40,
    ref_context: SourceRefContext | None = None,
) -> dict:
    triage = source_triage or SourceTriageReport()
    return {
        "ranked_sources": [
            {
                "source_ref": source_ref_for_id(item.source_id, ref_context) if ref_context else "S??",
                "rank": item.rank,
                "score": item.score,
                "useful_for": item.useful_for[:8],
                "evidence_ids": item.evidence_ids[:8],
                "top_quote_refs": item.top_quote_refs[:8],
                "relevance_reasons": item.relevance_reasons[:5],
                "cautions": item.cautions[:5],
            }
            for item in triage.ranked_sources[:source_limit]
        ],
        "top_sources_by_function": {
            key: source_refs(value[:10], ref_context) for key, value in triage.top_sources_by_function.items()
        },
        "top_evidence_by_function": {key: value[:10] for key, value in triage.top_evidence_by_function.items()},
        "gaps": triage.gaps[:12],
        "contradictions": triage.contradictions[:12],
        "source_clusters": [
            {
                "cluster_id": cluster.cluster_id,
                "label": cluster.label,
                "source_refs": source_refs(cluster.source_ids, ref_context),
                "primary_source_refs": source_refs(cluster.primary_source_ids, ref_context),
                "secondary_source_refs": source_refs(cluster.secondary_source_ids, ref_context),
                "duplicate_source_refs": source_refs(cluster.duplicate_source_ids, ref_context),
                "evidence_ids": cluster.evidence_ids[:10],
                "rationale": cluster.rationale,
            }
            for cluster in triage.source_clusters[:20]
        ],
        "source_relationships": [
            {
                "source_ref": source_ref_for_id(rel.source_id, ref_context) if ref_context else "S??",
                "related_source_ref": source_ref_for_id(rel.related_source_id, ref_context) if ref_context else "S??",
                "relationship": rel.relationship,
                "evidence_ids": rel.evidence_ids[:10],
                "rationale": rel.rationale,
            }
            for rel in triage.source_relationships[:40]
        ],
    }


def dedupe_strings(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def quote_matches_requirements(quote: QuoteCard, refs: set[str]) -> bool:
    return bool((quote.evidence_id and quote.evidence_id in refs) or quote_ref(quote) in refs or quote.locator in refs)


__all__ = [
    "evidence_inventory",
    "evidence_metadata_flags",
    "trim",
]
