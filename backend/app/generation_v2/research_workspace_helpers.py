from __future__ import annotations

from typing import Any

from app.generation_v2.research_workspace_core_helpers import (
    active_atom_drafts,
    active_job_atom_count,
    atom_kind,
    coerce_atom_kind,
    confidence,
    coverage_status,
    dedupe,
    dedupe_normalized,
    now,
    parse_dt,
    source_map_signature,
    stable_hash,
    stable_id,
    story_weight,
)
from app.generation_v2.research_workspace_gap_helpers import (
    gap_key,
    merge_research_gap,
    normalized_question_key,
)

SOURCE_QUALITY_PATTERNS = (
    "source quality",
    "source-quality",
    "источник-качество",
    "weak support",
    "needs corroboration",
    "need corroboration",
    "missing corroboration",
    "secondary confirmation",
    "redundant secondary",
    "source coverage",
    "weakly sourced",
    "нужно добрать источник",
    "требует добора",
)

def merge_atom_drafts_explicit(primary: dict[str, Any], duplicates: list[dict[str, Any]], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    all_drafts = [primary, *duplicates]
    for key in ("source_refs", "source_ids", "quote_refs", "anchor_refs", "evidence_ids"):
        merged[key] = dedupe([str(value) for draft in all_drafts for value in (draft.get(key) or []) if str(value).strip()])
    for key in ("title", "claim", "explanation", "story_value", "risk", "treatment_role_hint"):
        value = str(overrides.get(key) or "").strip()
        if value:
            merged[key] = value
    if str(overrides.get("source_coverage_status") or "").strip():
        merged["source_coverage_status"] = coverage_status(str(overrides.get("source_coverage_status") or ""))
    if str(overrides.get("story_weight") or "").strip():
        merged["story_weight"] = story_weight(str(overrides.get("story_weight") or ""), kind=str(merged.get("kind") or ""))
    try:
        merged["confidence"] = max(float(draft.get("confidence") or 0.0) for draft in all_drafts)
    except (TypeError, ValueError):
        pass
    merged["updated_at"] = now()
    merged["merged_atom_ids"] = dedupe(
        [
            str(draft.get("atom_id") or "")
            for draft in all_drafts
            if str(draft.get("atom_id") or "").strip()
        ]
        + [
            str(atom_id)
            for draft in all_drafts
            for atom_id in (draft.get("merged_atom_ids") or [])
            if str(atom_id).strip()
        ]
    )
    return merged


def looks_like_source_quality_note(*parts: str) -> bool:
    text = " ".join(str(part or "").lower() for part in parts)
    return any(pattern in text for pattern in SOURCE_QUALITY_PATTERNS)


def draft_is_source_quality_note(draft: dict[str, Any]) -> bool:
    return looks_like_source_quality_note(
        str(draft.get("title") or ""),
        str(draft.get("claim") or ""),
        str(draft.get("explanation") or ""),
        str(draft.get("story_value") or ""),
        str(draft.get("risk") or ""),
    )


def source_quality_risk_from_draft(draft: dict[str, Any]) -> str:
    title = str(draft.get("title") or "Source-quality note").strip()
    risk = str(draft.get("risk") or "").strip()
    return f"Source-quality note excluded from editorial atoms: {title}. {risk}".strip()


__all__ = [
    "active_atom_drafts",
    "active_job_atom_count",
    "atom_kind",
    "coerce_atom_kind",
    "confidence",
    "coverage_status",
    "dedupe",
    "dedupe_normalized",
    "draft_is_source_quality_note",
    "gap_key",
    "looks_like_source_quality_note",
    "merge_research_gap",
    "merge_atom_drafts_explicit",
    "normalized_question_key",
    "now",
    "parse_dt",
    "source_map_signature",
    "source_quality_risk_from_draft",
    "stable_hash",
    "stable_id",
    "story_weight",
]
