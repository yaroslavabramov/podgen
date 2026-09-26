from __future__ import annotations

from typing import Any

from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = model_preview_text(value, collapse_whitespace=False)
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
        "title": model_preview_text(card.title, collapse_whitespace=False),
        "domain": card.domain,
        "authority": card.authority,
        "coverage_status": card.coverage_status,
        "coverage_question_ids": card.coverage_question_ids,
        "editorial_functions": card.editorial_functions,
        "useful_for": card.useful_for,
        **_preview_fields("summary", card.summary, 700),
        "caution_previews": [_preview_fields("text", item, 400) for item in card.gaps_or_cautions],
        **_evidence_page_fields(card.evidence_ids, include_evidence_ids=include_evidence_ids, limit=8),
    }


def _quote_card_payload(card: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(card.source_id, context),
        **({"evidence_id": card.evidence_id} if include_evidence_ids and card.evidence_id else {}),
        "locator": model_preview_text(card.locator, collapse_whitespace=False),
        "narrative_use": model_preview_text(card.narrative_use, collapse_whitespace=False),
        **_preview_fields("quote", card.quote, 500),
    }


def _cluster_payload(cluster: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    def refs(values: list[Any]) -> list[str]:
        return [source_ref_for_id(str(value), context) for value in values if value]

    return {
        "cluster_id": cluster.cluster_id,
        "label": model_preview_text(cluster.label, collapse_whitespace=False),
        "source_refs": refs(cluster.source_ids),
        "primary_source_refs": refs(cluster.primary_source_ids),
        "secondary_source_refs": refs(cluster.secondary_source_ids),
        "duplicate_source_refs": refs(cluster.duplicate_source_ids),
        **_evidence_page_fields(cluster.evidence_ids, include_evidence_ids=include_evidence_ids, limit=10),
        **_preview_fields("rationale", cluster.rationale, 500),
    }


def _relationship_payload(relationship: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(relationship.source_id, context),
        "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
        "relationship": relationship.relationship,
        **_evidence_page_fields(
            relationship.evidence_ids,
            include_evidence_ids=include_evidence_ids,
            limit=10,
        ),
        **_preview_fields("rationale", relationship.rationale, 400),
    }


def _gap_record_payload(
    record: Any,
    context: Any,
    *,
    gap_ref: str,
    include_evidence_ids: bool,
) -> dict[str, Any]:
    return {
        "gap_ref": gap_ref,
        "kind": record.kind,
        "source_refs": [source_ref_for_id(source_id, context) for source_id in record.source_ids],
        "question_ids": list(record.question_ids),
        **_evidence_page_fields(
            record.evidence_ids,
            include_evidence_ids=include_evidence_ids,
            limit=10,
        ),
        **_preview_fields(
            "description",
            model_preview_text(
                record.description,
                replacements=context.ref_by_source_id,
                collapse_whitespace=False,
            ),
            600,
        ),
    }


def _evidence_page_fields(
    values: list[Any],
    *,
    include_evidence_ids: bool,
    limit: int | None,
) -> dict[str, Any]:
    evidence_ids = [str(item) for item in values if str(item).strip()]
    returned_ids = evidence_ids if limit is None else evidence_ids[:limit]
    return {
        **({"evidence_ids": returned_ids} if include_evidence_ids and evidence_ids else {}),
        "evidence_id_count": len(evidence_ids),
        "evidence_id_omitted_count": (
            max(0, len(evidence_ids) - len(returned_ids)) if include_evidence_ids else len(evidence_ids)
        ),
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
        "coverage_question_ids": list(card.coverage_question_ids),
        "editorial_functions": list(card.editorial_functions),
        **_evidence_page_fields(card.evidence_ids, include_evidence_ids=include_evidence_ids, limit=None),
    }


def _full_assessment_payload(assessment: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    source_ref = source_ref_for_id(assessment.source_id, context)
    return {
        "assessment_ref": f"assessment:{source_ref}",
        "source_ref": source_ref,
        "processing_status": assessment.processing_status,
        "representation_status": assessment.representation_status,
        "representation_reason": assessment.representation_reason,
        "relevance": assessment.relevance,
        "relevance_reason": assessment.relevance_reason,
        "authority": assessment.authority,
        "authority_reason": assessment.authority_reason,
        "uniqueness": assessment.uniqueness,
        "uniqueness_reason": assessment.uniqueness_reason,
        "disposition": assessment.disposition,
        "disposition_reason": assessment.disposition_reason,
        "coverage_question_ids": list(assessment.coverage_question_ids),
        "editorial_functions": list(assessment.editorial_functions),
        "limitations": list(assessment.limitations),
        "confidence": assessment.confidence,
        **_evidence_page_fields(
            assessment.evidence_ids,
            include_evidence_ids=include_evidence_ids,
            limit=None,
        ),
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
        **_evidence_page_fields(cluster.evidence_ids, include_evidence_ids=include_evidence_ids, limit=None),
    }


def _full_relationship_payload(relationship: Any, context: Any, *, include_evidence_ids: bool) -> dict[str, Any]:
    return {
        "source_ref": source_ref_for_id(relationship.source_id, context),
        "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
        "relationship": relationship.relationship,
        "rationale": relationship.rationale,
        **_evidence_page_fields(relationship.evidence_ids, include_evidence_ids=include_evidence_ids, limit=None),
    }


def _full_gap_record_payload(
    record: Any,
    context: Any,
    *,
    gap_ref: str,
    include_evidence_ids: bool,
) -> dict[str, Any]:
    return {
        "gap_ref": gap_ref,
        "kind": record.kind,
        "description": model_preview_text(
            record.description,
            replacements=context.ref_by_source_id,
            collapse_whitespace=False,
        ),
        "source_refs": [source_ref_for_id(source_id, context) for source_id in record.source_ids],
        "question_ids": list(record.question_ids),
        **_evidence_page_fields(
            record.evidence_ids,
            include_evidence_ids=include_evidence_ids,
            limit=None,
        ),
    }


def _sanitize_model_value(value: Any) -> Any:
    if isinstance(value, str):
        return model_preview_text(value, collapse_whitespace=False)
    if isinstance(value, list):
        return [_sanitize_model_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_model_value(item) for key, item in value.items()}
    return value


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
    structured_gap_keys = {
        " ".join(str(record.description).split()).casefold() for record in source_map.gap_records
    }
    all_gaps = _dedupe_preserving_text(
        [
            item
            for item in [
                *source_map.gaps,
                *source_map.source_triage.gaps,
                *source_map.contradictions,
                *source_map.source_triage.contradictions,
            ]
            if " ".join(str(item).split()).casefold() not in structured_gap_keys
        ]
    )
    all_gaps = [
        model_preview_text(item, replacements=context.ref_by_source_id, collapse_whitespace=False)
        for item in all_gaps
    ]
    gap_ref_by_id = {record.gap_id: public_gap_ref(record.gap_id) for record in source_map.gap_records}
    collections: dict[str, tuple[list[Any], Any]] = {
        "source_map_cards": (
            list(source_map.source_cards),
            lambda item: _full_source_card_payload(item, context, include_evidence_ids=include_evidence_ids),
        ),
        "source_map_assessments": (
            list(source_map.source_assessments),
            lambda item: _full_assessment_payload(
                item,
                context,
                include_evidence_ids=include_evidence_ids,
            ),
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
        "source_map_gap_records": (
            list(source_map.gap_records),
            lambda item: _full_gap_record_payload(
                item,
                context,
                gap_ref=gap_ref_by_id[item.gap_id],
                include_evidence_ids=include_evidence_ids,
            ),
        ),
    }
    if section not in collections:
        raise ValueError(f"Unknown SourceMap page section: {section}")
    values, serializer = collections[section]
    safe_cursor = max(0, int(cursor))
    safe_limit = max(1, min(int(limit), 40))
    page_values = values[safe_cursor : safe_cursor + safe_limit]
    items = [serializer(item) for item in page_values]
    return fit_model_page(
        _sanitize_model_value(
            {
                "section": section,
                "items": items,
                "page": _page_metadata(len(values), len(items), safe_limit, cursor=safe_cursor),
            }
        ),
        requested_limit=safe_limit,
    )


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
    assessment_limit = 80
    gap_limit = 40
    gap_record_limit = 40

    source_cards = [
        _source_card_payload(card, context, include_evidence_ids=include_evidence_ids)
        for card in source_map.source_cards[:source_card_limit]
    ]
    assessments = [
        {
            "source_ref": source_ref_for_id(item.source_id, context),
            "processing_status": item.processing_status,
            "representation_status": item.representation_status,
            "relevance": item.relevance,
            "authority": item.authority,
            "uniqueness": item.uniqueness,
            "disposition": item.disposition,
            "coverage_question_ids": item.coverage_question_ids,
            "editorial_functions": item.editorial_functions,
            "limitation_count": len(item.limitations),
            "evidence_id_count": len(item.evidence_ids),
        }
        for item in source_map.source_assessments[:assessment_limit]
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
    gap_records = [
        _gap_record_payload(
            item,
            context,
            gap_ref=public_gap_ref(item.gap_id),
            include_evidence_ids=include_evidence_ids,
        )
        for item in source_map.gap_records[:gap_record_limit]
    ]
    structured_gap_keys = {
        " ".join(str(record.description).split()).casefold() for record in source_map.gap_records
    }
    all_gaps = _dedupe(
        [
            model_preview_text(item, replacements=context.ref_by_source_id, collapse_whitespace=False)
            for item in [
                *source_map.gaps,
                *source_map.source_triage.gaps,
                *source_map.contradictions,
                *source_map.source_triage.contradictions,
            ]
            if " ".join(str(item).split()).casefold() not in structured_gap_keys
        ]
    )
    gaps = [_preview_fields("gap", item, 500) for item in all_gaps[:gap_limit]]
    return {
        "source_card_count": len(source_map.source_cards),
        "quote_card_count": len(source_map.quote_cards),
        "source_assessment_count": len(source_map.source_assessments),
        "cluster_count": len(source_map.source_triage.source_clusters),
        "relationship_count": len(source_map.source_triage.source_relationships),
        "gap_count": len(source_map.gap_records) + len(all_gaps),
        "legacy_gap_count": len(all_gaps),
        "gap_record_count": len(source_map.gap_records),
        "includes_evidence_ids": include_evidence_ids,
        "source_cards": source_cards,
        "source_assessments": assessments,
        "quote_cards": quote_cards,
        "clusters": clusters,
        "relationships": relationships,
        "gaps": gaps,
        "gap_records": gap_records,
        "pages": {
            "source_cards": _page_metadata(len(source_map.source_cards), len(source_cards), source_card_limit),
            "source_assessments": _page_metadata(
                len(source_map.source_assessments), len(assessments), assessment_limit
            ),
            "quote_cards": _page_metadata(len(source_map.quote_cards), len(quote_cards), quote_card_limit),
            "clusters": _page_metadata(len(source_map.source_triage.source_clusters), len(clusters), cluster_limit),
            "relationships": _page_metadata(
                len(source_map.source_triage.source_relationships), len(relationships), relationship_limit
            ),
            "gaps": _page_metadata(len(all_gaps), len(gaps), gap_limit),
            "source_map_gap_records": _page_metadata(
                len(source_map.gap_records), len(gap_records), gap_record_limit
            ),
        },
    }


def source_map_planner_summary(
    source_map: SourceMap,
    source_inventory: SourceMap | None = None,
) -> dict[str, Any]:
    """Compact routing index for the planner; full SourceMap items remain pageable."""

    context = build_source_ref_context(source_inventory or source_map)
    source_limit = 40
    assessment_limit = 40
    cluster_limit = 30
    relationship_limit = 30
    gap_limit = 20
    gap_record_limit = 20
    cards = list(source_map.source_cards)
    assessments = list(source_map.source_assessments)
    clusters = list(source_map.source_triage.source_clusters)
    relationships = list(source_map.source_triage.source_relationships)
    gaps = _dedupe_preserving_text(
        [
            model_preview_text(item, replacements=context.ref_by_source_id, collapse_whitespace=False)
            for item in [
                *source_map.gaps,
                *source_map.source_triage.gaps,
                *source_map.contradictions,
                *source_map.source_triage.contradictions,
            ]
            if " ".join(str(item).split()).casefold()
            not in {
                " ".join(str(record.description).split()).casefold()
                for record in source_map.gap_records
            }
        ]
    )
    return {
        "source_card_count": len(cards),
        "source_assessment_count": len(assessments),
        "quote_card_count": len(source_map.quote_cards),
        "cluster_count": len(clusters),
        "relationship_count": len(relationships),
        "gap_count": len(source_map.gap_records) + len(gaps),
        "legacy_gap_count": len(gaps),
        "gap_record_count": len(source_map.gap_records),
        "source_index": [
            {
                "source_ref": source_ref_for_id(item.source_id, context),
                **_preview_fields("title", item.title, 180),
                "domain": item.domain,
                "coverage_status": item.coverage_status,
                "coverage_question_ids": item.coverage_question_ids,
                "editorial_functions": item.editorial_functions,
            }
            for item in cards[:source_limit]
        ],
        "assessment_index": [
            {
                "source_ref": source_ref_for_id(item.source_id, context),
                "processing_status": item.processing_status,
                "representation_status": item.representation_status,
                "relevance": item.relevance,
                "authority": item.authority,
                "uniqueness": item.uniqueness,
                "disposition": item.disposition,
                "coverage_question_ids": item.coverage_question_ids,
                "editorial_functions": item.editorial_functions,
                "limitation_count": len(item.limitations),
            }
            for item in assessments[:assessment_limit]
        ],
        "cluster_index": [
            compact_cluster_for_prompt(_cluster_payload(item, context, include_evidence_ids=False))
            for item in clusters[:cluster_limit]
        ],
        "relationship_index": [
            {
                "source_ref": source_ref_for_id(item.source_id, context),
                "related_source_ref": source_ref_for_id(item.related_source_id, context),
                "relationship": item.relationship,
            }
            for item in relationships[:relationship_limit]
        ],
        "gap_previews": [_preview_fields("gap", item, 260) for item in gaps[:gap_limit]],
        "pages": {
            "source_map_cards": _page_metadata(len(cards), min(len(cards), source_limit), source_limit),
            "source_map_assessments": _page_metadata(
                len(assessments), min(len(assessments), assessment_limit), assessment_limit
            ),
            "source_map_quotes": _page_metadata(len(source_map.quote_cards), 0, 1),
            "source_map_clusters": _page_metadata(
                len(clusters), min(len(clusters), cluster_limit), cluster_limit
            ),
            "source_map_relationships": _page_metadata(
                len(relationships), min(len(relationships), relationship_limit), relationship_limit
            ),
            "source_map_gaps": _page_metadata(len(gaps), min(len(gaps), gap_limit), gap_limit),
            "source_map_gap_records": _page_metadata(
                len(source_map.gap_records),
                min(len(source_map.gap_records), gap_record_limit),
                gap_record_limit,
            ),
        },
    }


def source_map_summary_for_job(source_map_summary: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    refs = set(str(item) for item in job.get("source_refs") or [] if str(item).strip())
    catalog_pages = _job_catalog_pages(source_map_summary.get("pages") or {})
    return {
        "source_card_count": source_map_summary.get("source_card_count", 0),
        "quote_card_count": source_map_summary.get("quote_card_count", 0),
        "source_assessment_count": source_map_summary.get("source_assessment_count", 0),
        "gap_count": source_map_summary.get("gap_count", 0),
        "legacy_gap_count": source_map_summary.get("legacy_gap_count", 0),
        "gap_record_count": source_map_summary.get("gap_record_count", 0),
        "cluster_count": source_map_summary.get("cluster_count", len(source_map_summary.get("clusters") or [])),
        "relationship_count": source_map_summary.get(
            "relationship_count", len(source_map_summary.get("relationships") or [])
        ),
        "includes_evidence_ids": bool(source_map_summary.get("includes_evidence_ids")),
        "filtered_for_source_refs": sorted(refs),
        "allow_broad_search": bool(job.get("allow_broad_search")),
        "pages": catalog_pages,
        "usage_note": (
            "Job prompt intentionally carries counts/page metadata only. Use list_research_workspace_items "
            "source_map_* sections on demand for a specific source, requirement, or conflict."
        ),
    }


def _job_catalog_pages(pages: dict[str, Any]) -> dict[str, Any]:
    section_by_key = {
        "source_cards": "source_map_cards",
        "source_map_cards": "source_map_cards",
        "source_assessments": "source_map_assessments",
        "source_map_assessments": "source_map_assessments",
        "quote_cards": "source_map_quotes",
        "source_map_quotes": "source_map_quotes",
        "clusters": "source_map_clusters",
        "source_map_clusters": "source_map_clusters",
        "relationships": "source_map_relationships",
        "source_map_relationships": "source_map_relationships",
        "gaps": "source_map_gaps",
        "source_map_gaps": "source_map_gaps",
        "source_map_gap_records": "source_map_gap_records",
    }
    result: dict[str, Any] = {}
    for key, section in section_by_key.items():
        page = pages.get(key)
        if not isinstance(page, dict) or section in result:
            continue
        total = int(page.get("total_count") or 0)
        page_limit = min(40, max(1, int(page.get("page_limit") or page.get("requested_limit") or 40)))
        result[section] = {
            **page,
            "total_count": total,
            "returned_count": 0,
            "omitted_count": total,
            "cursor": "0",
            "next_cursor": "0" if total > 0 else "",
            "page_limit": page_limit,
            "initial_payload_includes_items": False,
        }
    return result


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
    "source_map_planner_summary",
    "source_map_research_summary",
    "source_map_summary_for_job",
]
