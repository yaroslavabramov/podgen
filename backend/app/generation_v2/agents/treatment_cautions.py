from __future__ import annotations

import re
from typing import Any

from app.generation_v2.agents.editorial_context import trim as _trim
from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_refs import SourceRefContext, source_ref_for_id


LOW_VALUE_TREATMENT_CAUTION_FRAGMENTS = (
    "read window",
    "skimmed window",
    "narrower window",
    "extracted text",
    "ocr",
    "summary excerpt",
    "quote carefully",
)


def source_note_ref_pair(source_id: str, related_source_id: str, context: SourceRefContext) -> tuple[str, str] | None:
    left = source_ref_for_id(source_id, context)
    right = source_ref_for_id(related_source_id, context)
    if "S??" in {left, right}:
        return None
    return left, right


def treatment_relevant_caution(text: str) -> str:
    text = _trim(text, 320)
    if len(text) < 45:
        return ""
    lowered = text.lower()
    if any(fragment in lowered for fragment in LOW_VALUE_TREATMENT_CAUTION_FRAGMENTS):
        return ""
    return text


def add_source_caution(
    cautions: list[dict[str, Any]],
    seen: set[tuple[str, tuple[str, ...], str]],
    *,
    category: str,
    source_refs: list[str],
    note: str,
) -> None:
    refs = tuple(sorted({ref for ref in source_refs if ref != "S??"}))
    note = treatment_relevant_caution(note)
    if not refs or not note:
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
    inventory_cards = {card.source_id: card for card in source_inventory.source_cards}
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
        rationale = _trim(relationship.rationale, 320)
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
            refs = ", ".join(cluster_refs[:8])
            duplicate_note = f"; duplicates: {', '.join(duplicate_refs[:6])}" if duplicate_refs else ""
            add_source_caution(
                cautions,
                seen,
                category="cluster",
                source_refs=cluster_refs[:8],
                note=f"{cluster.label}: {refs}{duplicate_note}. {_trim(cluster.rationale, 280)}".strip(),
            )
    for card in [*source_map.source_cards, *inventory_cards.values()]:
        source_ref = source_ref_for_id(card.source_id, ref_context)
        if source_ref not in referenced_refs:
            continue
        for caution in card.gaps_or_cautions[:2]:
            add_source_caution(
                cautions,
                seen,
                category="source",
                source_refs=[source_ref],
                note=str(caution),
            )
    ref_pattern = re.compile(r"\bS\d+\b")
    for gap in [*source_map.gaps, *source_map.source_triage.gaps, *source_map.contradictions]:
        text = _trim(str(gap or ""), 360)
        mentioned_refs = set(ref_pattern.findall(text))
        if text and mentioned_refs and mentioned_refs & referenced_refs:
            add_source_caution(
                cautions,
                seen,
                category="corpus",
                source_refs=sorted(mentioned_refs & referenced_refs),
                note=text,
            )
    return cautions[:12]


__all__ = [
    "add_source_caution",
    "source_note_ref_pair",
    "treatment_relevant_caution",
    "treatment_source_cautions",
]
