from __future__ import annotations

import re
from typing import Any

from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_refs import SourceRefContext, source_ref_for_id


def source_note_ref_pair(source_id: str, related_source_id: str, context: SourceRefContext) -> tuple[str, str] | None:
    left = source_ref_for_id(source_id, context)
    right = source_ref_for_id(related_source_id, context)
    if "S??" in {left, right}:
        return None
    return left, right


def treatment_relevant_caution(text: str) -> str:
    return str(text or "").strip()


def add_source_caution(
    cautions: list[dict[str, Any]],
    seen: set[tuple[str, tuple[str, ...], str]],
    *,
    category: str,
    source_refs: list[str],
    note: str,
    allow_global: bool = False,
) -> None:
    refs = tuple(sorted({ref for ref in source_refs if ref != "S??"}))
    note = treatment_relevant_caution(note)
    if (not refs and not allow_global) or not note:
        return
    key = (category, refs, note.lower())
    if key in seen:
        return
    seen.add(key)
    cautions.append({"category": category, "source_refs": list(refs), "note": note})


def treatment_source_cautions(
    *,
    source_map: SourceMap,
    source_inventory: SourceMap,
    referenced_refs: set[str],
    ref_context: SourceRefContext,
) -> list[dict[str, Any]]:
    cautions: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    inventory_cautions_by_source = {
        card.source_id: {str(item).strip().casefold() for item in card.gaps_or_cautions if str(item).strip()}
        for card in source_inventory.source_cards
    }
    for relationship in source_map.source_triage.source_relationships:
        refs = source_note_ref_pair(relationship.source_id, relationship.related_source_id, ref_context)
        if refs is None or not (set(refs) & referenced_refs):
            continue
        if relationship.relationship not in {
            "duplicate",
            "near_duplicate",
            "primary_to_secondary",
            "contradicts",
            "updates",
        }:
            continue
        rationale = str(relationship.rationale or "").strip()
        add_source_caution(
            cautions,
            seen,
            category=relationship.relationship,
            source_refs=list(refs),
            note=rationale or f"{refs[0]} is {relationship.relationship} with {refs[1]}.",
        )
    for cluster in source_map.source_triage.source_clusters:
        cluster_refs = [source_ref_for_id(source_id, ref_context) for source_id in cluster.source_ids]
        cluster_refs = [ref for ref in cluster_refs if ref != "S??"]
        if not set(cluster_refs) & referenced_refs:
            continue
        duplicate_refs = [source_ref_for_id(source_id, ref_context) for source_id in cluster.duplicate_source_ids]
        duplicate_refs = [ref for ref in duplicate_refs if ref != "S??"]
        if duplicate_refs or cluster.rationale:
            refs = ", ".join(cluster_refs)
            duplicate_note = f"; duplicates: {', '.join(duplicate_refs)}" if duplicate_refs else ""
            add_source_caution(
                cautions,
                seen,
                category="cluster",
                source_refs=cluster_refs,
                note=f"{cluster.label}: {refs}{duplicate_note}. {cluster.rationale}".strip(),
            )
    for card in source_map.source_cards:
        source_ref = source_ref_for_id(card.source_id, ref_context)
        if source_ref not in referenced_refs:
            continue
        for caution in card.gaps_or_cautions:
            if str(caution).strip().casefold() in inventory_cautions_by_source.get(card.source_id, set()):
                continue
            add_source_caution(
                cautions,
                seen,
                category="source",
                source_refs=[source_ref],
                note=str(caution),
            )
    for assessment in source_map.source_assessments:
        source_ref = source_ref_for_id(assessment.source_id, ref_context)
        if source_ref not in referenced_refs:
            continue
        for limitation in assessment.limitations:
            add_source_caution(
                cautions,
                seen,
                category="source_limitation",
                source_refs=[source_ref],
                note=str(limitation),
            )
        if assessment.representation_status in {
            "partial",
            "unreadable",
            "not_target_content",
        } and assessment.representation_reason:
            add_source_caution(
                cautions,
                seen,
                category="representation_limitation",
                source_refs=[source_ref],
                note=assessment.representation_reason,
            )
    structured_gap_texts: set[str] = set()
    for gap in source_map.gap_records:
        text = str(gap.description or "").strip()
        structured_gap_texts.add(text.casefold())
        source_refs = [source_ref_for_id(source_id, ref_context) for source_id in gap.source_ids]
        source_refs = [source_ref for source_ref in source_refs if source_ref != "S??"]
        if text and (not source_refs or set(source_refs) & referenced_refs):
            add_source_caution(
                cautions,
                seen,
                category=f"corpus_{gap.kind or 'gap'}",
                source_refs=source_refs,
                note=text,
                allow_global=not source_refs,
            )
    ref_pattern = re.compile(r"\bS\d+\b")
    for gap in [
        *source_map.gaps,
        *source_map.source_triage.gaps,
        *source_map.contradictions,
        *source_map.source_triage.contradictions,
    ]:
        text = str(gap or "").strip()
        if text.casefold() in structured_gap_texts:
            continue
        mentioned_refs = set(ref_pattern.findall(text))
        if text and mentioned_refs and mentioned_refs & referenced_refs:
            add_source_caution(
                cautions,
                seen,
                category="corpus",
                source_refs=sorted(mentioned_refs & referenced_refs),
                note=text,
            )
    return cautions


__all__ = [
    "add_source_caution",
    "source_note_ref_pair",
    "treatment_relevant_caution",
    "treatment_source_cautions",
]
