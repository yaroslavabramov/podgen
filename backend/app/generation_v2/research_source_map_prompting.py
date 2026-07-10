from __future__ import annotations

from typing import Any

from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


def source_map_research_summary(
    source_map: SourceMap,
    source_inventory: SourceMap | None = None,
    *,
    include_evidence_ids: bool = True,
) -> dict[str, Any]:
    context = build_source_ref_context(source_inventory or source_map)

    def refs(source_ids: list[str]) -> list[str]:
        return [source_ref_for_id(str(source_id), context) for source_id in source_ids if source_id]

    source_cards = [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            "title": card.title,
            "domain": card.domain,
            "useful_for": card.useful_for[:8],
            "summary": card.summary[:700],
            "cautions": card.gaps_or_cautions[:6],
            **({"evidence_ids": card.evidence_ids[:8]} if include_evidence_ids and card.evidence_ids else {}),
        }
        for card in source_map.source_cards[:80]
    ]
    quote_cards = [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            **({"evidence_id": card.evidence_id} if include_evidence_ids and card.evidence_id else {}),
            "locator": card.locator,
            "narrative_use": card.narrative_use,
            "quote_excerpt": card.quote[:500],
        }
        for card in source_map.quote_cards[:60]
    ]
    clusters = [
        {
            "cluster_id": cluster.cluster_id,
            "label": cluster.label,
            "source_refs": refs(cluster.source_ids),
            "primary_source_refs": refs(cluster.primary_source_ids),
            "secondary_source_refs": refs(cluster.secondary_source_ids),
            "duplicate_source_refs": refs(cluster.duplicate_source_ids),
            **({"evidence_ids": cluster.evidence_ids[:10]} if include_evidence_ids and cluster.evidence_ids else {}),
            "rationale": cluster.rationale[:500],
        }
        for cluster in source_map.source_triage.source_clusters[:40]
    ]
    relationships = [
        {
            "source_ref": source_ref_for_id(relationship.source_id, context),
            "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
            "relationship": relationship.relationship,
            **({"evidence_ids": relationship.evidence_ids[:10]} if include_evidence_ids and relationship.evidence_ids else {}),
            "rationale": relationship.rationale[:400],
        }
        for relationship in source_map.source_triage.source_relationships[:60]
    ]
    gaps = _dedupe([*source_map.gaps[:20], *source_map.source_triage.gaps[:20]])
    return {
        "source_card_count": len(source_map.source_cards),
        "quote_card_count": len(source_map.quote_cards),
        "gap_count": len(gaps),
        "includes_evidence_ids": include_evidence_ids,
        "source_cards": source_cards,
        "quote_cards": quote_cards,
        "clusters": clusters,
        "relationships": relationships,
        "gaps": gaps,
    }


def source_map_summary_for_job(source_map_summary: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    refs = set(str(item) for item in job.get("source_refs") or [] if str(item).strip())
    base = {
        "source_card_count": source_map_summary.get("source_card_count", 0),
        "quote_card_count": source_map_summary.get("quote_card_count", 0),
        "gap_count": source_map_summary.get("gap_count", 0),
        "cluster_count": len(source_map_summary.get("clusters") or []),
        "relationship_count": len(source_map_summary.get("relationships") or []),
        "includes_evidence_ids": bool(source_map_summary.get("includes_evidence_ids")),
        "filtered_for_source_refs": sorted(refs),
        "allow_broad_search": bool(job.get("allow_broad_search")),
    }
    if not refs:
        return {
            **base,
            "source_cards": [],
            "quote_cards": [],
            "clusters": [compact_cluster_for_prompt(item) for item in (source_map_summary.get("clusters") or [])[:16]],
            "relationships": [],
            "gaps": [str(item)[:360] for item in (source_map_summary.get("gaps") or [])[:10]],
        }

    def has_ref(item: dict[str, Any], keys: list[str]) -> bool:
        item_refs: set[str] = set()
        for key in keys:
            value = item.get(key)
            if isinstance(value, list):
                item_refs.update(str(ref) for ref in value if str(ref).strip())
            elif value:
                item_refs.add(str(value))
        return bool(item_refs & refs)

    return {
        **base,
        "source_cards": [
            item
            for item in source_map_summary.get("source_cards") or []
            if str(item.get("source_ref") or "") in refs
        ],
        "quote_cards": [
            item
            for item in source_map_summary.get("quote_cards") or []
            if str(item.get("source_ref") or "") in refs
        ],
        "clusters": [
            item
            for item in source_map_summary.get("clusters") or []
            if has_ref(item, ["source_refs", "primary_source_refs", "secondary_source_refs", "duplicate_source_refs"])
        ],
        "relationships": [
            item
            for item in source_map_summary.get("relationships") or []
            if has_ref(item, ["source_ref", "related_source_ref"])
        ],
        "gaps": [str(item)[:360] for item in (source_map_summary.get("gaps") or [])[:10]],
    }


def compact_cluster_for_prompt(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster_id": cluster.get("cluster_id"),
        "label": cluster.get("label"),
        "source_refs": (cluster.get("source_refs") or [])[:10],
        "primary_source_refs": (cluster.get("primary_source_refs") or [])[:8],
        "duplicate_source_refs": (cluster.get("duplicate_source_refs") or [])[:8],
    }


__all__ = [
    "compact_cluster_for_prompt",
    "source_map_research_summary",
    "source_map_summary_for_job",
]
