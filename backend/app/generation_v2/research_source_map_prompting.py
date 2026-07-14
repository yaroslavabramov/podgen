from __future__ import annotations

import json
from typing import Any

from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.schemas import SourceMap
from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = str(value or "")
    return {
        f"{name}_preview": text[:limit],
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _page_metadata(total: int, returned: int, limit: int, *, cursor: int = 0) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
        "page_limit": limit,
    }


def _source_card_payload(card: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(card.source_id, context),
        "title": card.title,
        "domain": card.domain,
        "useful_for": card.useful_for,
        **_preview_fields("summary", card.summary, 700),
        "caution_previews": [_preview_fields("text", item, 400) for item in card.gaps_or_cautions],
        **({"evidence_ids": card.evidence_ids[:8]} if include_evidence_ids and card.evidence_ids else {}),
        "evidence_id_count": len(card.evidence_ids),
    }


def _quote_card_payload(card: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(card.source_id, context),
        **({"evidence_id": card.evidence_id} if include_evidence_ids and card.evidence_id else {}),
        "locator": card.locator,
        "narrative_use": card.narrative_use,
        **_preview_fields("quote", card.quote, 500),
    }


def _cluster_payload(cluster: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    def refs(values: list[Any]) -> list[str]:
        return [source_ref_for_id(str(value), context) for value in values if value]

    return {
        "cluster_id": cluster.cluster_id,
        "label": cluster.label,
        "source_refs": refs(cluster.source_ids),
        "primary_source_refs": refs(cluster.primary_source_ids),
        "secondary_source_refs": refs(cluster.secondary_source_ids),
        "duplicate_source_refs": refs(cluster.duplicate_source_ids),
        **({"evidence_ids": cluster.evidence_ids[:10]} if include_evidence_ids and cluster.evidence_ids else {}),
        "evidence_id_count": len(cluster.evidence_ids),
        **_preview_fields("rationale", cluster.rationale, 500),
    }


def _relationship_payload(relationship: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(relationship.source_id, context),
        "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
        "relationship": relationship.relationship,
        **(
            {"evidence_ids": relationship.evidence_ids[:10]}
            if include_evidence_ids and relationship.evidence_ids
            else {}
        ),
        "evidence_id_count": len(relationship.evidence_ids),
        **_preview_fields("rationale", relationship.rationale, 400),
    }


def _evidence_page_fields(values: list[Any], *, include_evidence_ids: bool, limit: int) -> dict[str, Any]:
    evidence_ids = [str(item) for item in values if str(item).strip()]
    return {
        **({"evidence_ids": evidence_ids[:limit]} if include_evidence_ids and evidence_ids else {}),
        "evidence_id_count": len(evidence_ids),
        "evidence_id_omitted_count": max(0, len(evidence_ids) - limit) if include_evidence_ids else len(evidence_ids),
    }


def _full_source_card_payload(card: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(card.source_id, context),
        "title": card.title,
        "source_type": card.source_type,
        "domain": card.domain,
        "published_at": card.published_at,
        "length_hint": card.length_hint,
        "reliability": card.reliability,
        "authority": card.authority,
        "summary": card.summary,
        "useful_for": list(card.useful_for),
        "gaps_or_cautions": list(card.gaps_or_cautions),
        "representation_summary": card.representation_summary,
        "ready_representation_count": card.ready_representation_count,
        "failed_representation_count": card.failed_representation_count,
        "coverage_status": card.coverage_status,
        **_evidence_page_fields(card.evidence_ids, include_evidence_ids=include_evidence_ids, limit=8),
    }


def _full_quote_card_payload(card: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(card.source_id, context),
        **({"evidence_id": card.evidence_id} if include_evidence_ids and card.evidence_id else {}),
        "has_evidence": bool(card.evidence_id),
        "quote": card.quote,
        "locator": card.locator,
        "narrative_use": card.narrative_use,
        "source_kind": card.source_kind,
        "confidence": card.confidence,
        "confidence_reason": card.confidence_reason,
    }


def _full_cluster_payload(cluster: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    def refs(values: list[Any]) -> list[str]:
        return [source_ref_for_id(str(value), context) for value in values if value]

    return {
        "cluster_id": cluster.cluster_id,
        "label": cluster.label,
        "source_refs": refs(cluster.source_ids),
        "primary_source_refs": refs(cluster.primary_source_ids),
        "secondary_source_refs": refs(cluster.secondary_source_ids),
        "duplicate_source_refs": refs(cluster.duplicate_source_ids),
        "rationale": cluster.rationale,
        **_evidence_page_fields(cluster.evidence_ids, include_evidence_ids=include_evidence_ids, limit=10),
    }


def _full_relationship_payload(relationship: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(relationship.source_id, context),
        "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
        "relationship": relationship.relationship,
        "rationale": relationship.rationale,
        **_evidence_page_fields(relationship.evidence_ids, include_evidence_ids=include_evidence_ids, limit=10),
    }


def _checked_full_page(payload: dict[str, Any]) -> dict[str, Any]:
    # Callers add the accepted flag before the runtime contract check.
    encoded = json.dumps({"accepted": True, **payload}, ensure_ascii=False, default=str)
    if len(encoded) > MODEL_TOOL_OUTPUT_MAX_CHARS:
        section = str(payload.get("section") or "source_map")
        raise ValueError(
            f"{section} persisted item exceeds the {MODEL_TOOL_OUTPUT_MAX_CHARS}-character model-read contract"
        )
    return payload


def _dedupe_preserving_text(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        key = text.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def source_map_research_page(
    source_map: SourceMap,
    source_inventory: SourceMap | None = None,
    *,
    section: str,
    cursor: int = 0,
    limit: int = 20,
    include_evidence_ids: bool = True,
) -> dict[str, Any]:
    context = build_source_ref_context(source_inventory or source_map)
    all_gaps = _dedupe_preserving_text([*source_map.gaps, *source_map.source_triage.gaps])
    collections: dict[str, tuple[list[Any], Any]] = {
        "source_map_cards": (
            list(source_map.source_cards),
            lambda item: _full_source_card_payload(item, context, include_evidence_ids=include_evidence_ids),
        ),
        "source_map_quotes": (
            list(source_map.quote_cards),
            lambda item: _full_quote_card_payload(item, context, include_evidence_ids=include_evidence_ids),
        ),
        "source_map_clusters": (
            list(source_map.source_triage.source_clusters),
            lambda item: _full_cluster_payload(item, context, include_evidence_ids=include_evidence_ids),
        ),
        "source_map_relationships": (
            list(source_map.source_triage.source_relationships),
            lambda item: _full_relationship_payload(item, context, include_evidence_ids=include_evidence_ids),
        ),
        "source_map_gaps": (all_gaps, lambda item: {"gap": str(item)}),
    }
    if section not in collections:
        raise ValueError(f"Unknown SourceMap page section: {section}")
    values, serializer = collections[section]
    safe_cursor = max(0, int(cursor))
    # Full validated items are deliberately one-per-page so the runtime's
    # model-facing output contract cannot silently cut semantic fields.
    safe_limit = 1
    page_values = values[safe_cursor : safe_cursor + safe_limit]
    items = [serializer(item) for item in page_values]
    return _checked_full_page({
        "section": section,
        "items": items,
        "page": _page_metadata(len(values), len(items), safe_limit, cursor=safe_cursor),
    })


def source_map_research_summary(
    source_map: SourceMap,
    source_inventory: SourceMap | None = None,
    *,
    include_evidence_ids: bool = True,
) -> dict[str, Any]:
    context = build_source_ref_context(source_inventory or source_map)
    source_card_limit = 80
    quote_card_limit = 60
    cluster_limit = 40
    relationship_limit = 60
    gap_limit = 40

    source_cards = [
        _source_card_payload(card, context, include_evidence_ids=include_evidence_ids)
        for card in source_map.source_cards[:source_card_limit]
    ]
    quote_cards = [
        _quote_card_payload(card, context, include_evidence_ids=include_evidence_ids)
        for card in source_map.quote_cards[:quote_card_limit]
    ]
    clusters = [
        _cluster_payload(cluster, context, include_evidence_ids=include_evidence_ids)
        for cluster in source_map.source_triage.source_clusters[:cluster_limit]
    ]
    relationships = [
        _relationship_payload(relationship, context, include_evidence_ids=include_evidence_ids)
        for relationship in source_map.source_triage.source_relationships[:relationship_limit]
    ]
    all_gaps = _dedupe([*source_map.gaps, *source_map.source_triage.gaps])
    gaps = [_preview_fields("gap", item, 500) for item in all_gaps[:gap_limit]]
    return {
        "source_card_count": len(source_map.source_cards),
        "quote_card_count": len(source_map.quote_cards),
        "cluster_count": len(source_map.source_triage.source_clusters),
        "relationship_count": len(source_map.source_triage.source_relationships),
        "gap_count": len(all_gaps),
        "includes_evidence_ids": include_evidence_ids,
        "source_cards": source_cards,
        "quote_cards": quote_cards,
        "clusters": clusters,
        "relationships": relationships,
        "gaps": gaps,
        "pages": {
            "source_cards": _page_metadata(len(source_map.source_cards), len(source_cards), source_card_limit),
            "quote_cards": _page_metadata(len(source_map.quote_cards), len(quote_cards), quote_card_limit),
            "clusters": _page_metadata(
                len(source_map.source_triage.source_clusters), len(clusters), cluster_limit
            ),
            "relationships": _page_metadata(
                len(source_map.source_triage.source_relationships), len(relationships), relationship_limit
            ),
            "gaps": _page_metadata(len(all_gaps), len(gaps), gap_limit),
        },
    }


def source_map_summary_for_job(source_map_summary: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    refs = set(str(item) for item in job.get("source_refs") or [] if str(item).strip())
    base = {
        "source_card_count": source_map_summary.get("source_card_count", 0),
        "quote_card_count": source_map_summary.get("quote_card_count", 0),
        "gap_count": source_map_summary.get("gap_count", 0),
        "cluster_count": source_map_summary.get("cluster_count", len(source_map_summary.get("clusters") or [])),
        "relationship_count": source_map_summary.get(
            "relationship_count", len(source_map_summary.get("relationships") or [])
        ),
        "includes_evidence_ids": bool(source_map_summary.get("includes_evidence_ids")),
        "filtered_for_source_refs": sorted(refs),
        "allow_broad_search": bool(job.get("allow_broad_search")),
        "pages": source_map_summary.get("pages") or {},
    }
    if not refs:
        return {
            **base,
            "source_cards": [],
            "quote_cards": [],
            "clusters": [compact_cluster_for_prompt(item) for item in (source_map_summary.get("clusters") or [])[:16]],
            "relationships": [],
            "gaps": (source_map_summary.get("gaps") or [])[:10],
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
        "gaps": (source_map_summary.get("gaps") or [])[:10],
    }


def compact_cluster_for_prompt(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster_id": cluster.get("cluster_id"),
        "label": cluster.get("label"),
        "source_refs": cluster.get("source_refs") or [],
        "primary_source_refs": cluster.get("primary_source_refs") or [],
        "duplicate_source_refs": cluster.get("duplicate_source_refs") or [],
    }


__all__ = [
    "compact_cluster_for_prompt",
    "source_map_research_page",
    "source_map_research_summary",
    "source_map_summary_for_job",
]
