from __future__ import annotations

import re
from typing import Any

from app.generation_v2.contracts.research import (
    ResearchGapCategory,
    ResearchGapSeverity,
    ResearchGapStatus,
    ResearchGapTreatmentRelevance,
)
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_GAP_CATEGORIES,
    RESEARCH_GAP_SEVERITIES,
    RESEARCH_GAP_STATUSES,
    RESEARCH_GAP_TREATMENT_RELEVANCE,
)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def dedupe_gaps(values: list[Any]) -> list[dict[str, Any]]:
    order: list[str] = []
    merged_by_key: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not question or category not in RESEARCH_GAP_CATEGORIES:
            continue
        gap = dict(raw)
        gap["category"] = category
        gap["severity"] = (
            str(gap.get("severity") or ResearchGapSeverity.NON_BLOCKING.value)
            if str(gap.get("severity") or "") in RESEARCH_GAP_SEVERITIES
            else ResearchGapSeverity.NON_BLOCKING.value
        )
        gap["status"] = (
            str(gap.get("status") or ResearchGapStatus.OPEN.value)
            if str(gap.get("status") or "") in RESEARCH_GAP_STATUSES
            else ResearchGapStatus.OPEN.value
        )
        gap["treatment_relevance"] = treatment_relevance(gap.get("treatment_relevance"))
        gap["normalized_key"] = str(gap.get("normalized_key") or gap_key(category, question))
        key = gap["normalized_key"]
        if key not in merged_by_key:
            order.append(key)
            merged_by_key[key] = gap
            continue
        merged_by_key[key] = merge_research_gap(merged_by_key[key], gap)
    return [merged_by_key[key] for key in order]


def merge_research_gap(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return incoming
    merged = dict(existing)
    incoming_status = str(incoming.get("status") or "open")
    incoming_resolution = str(incoming.get("resolution_note") or "").strip()
    severity_rank = {
        ResearchGapSeverity.OPTIONAL.value: 0,
        ResearchGapSeverity.NON_BLOCKING.value: 1,
        ResearchGapSeverity.BLOCKING.value: 2,
    }
    relevance_rank = {
        ResearchGapTreatmentRelevance.MINOR.value: 0,
        ResearchGapTreatmentRelevance.SUPPORTING.value: 1,
        ResearchGapTreatmentRelevance.CENTRAL.value: 2,
    }
    existing_severity = str(existing.get("severity") or ResearchGapSeverity.NON_BLOCKING.value)
    incoming_severity = str(incoming.get("severity") or ResearchGapSeverity.NON_BLOCKING.value)
    strongest_severity = existing_severity if severity_rank.get(existing_severity, 1) > severity_rank.get(incoming_severity, 1) else incoming_severity
    existing_relevance = treatment_relevance(existing.get("treatment_relevance"))
    incoming_relevance = treatment_relevance(incoming.get("treatment_relevance"))
    strongest_relevance = (
        existing_relevance
        if relevance_rank.get(existing_relevance, 1) > relevance_rank.get(incoming_relevance, 1)
        else incoming_relevance
    )
    existing_question = str(existing.get("question") or "").strip()
    source_refs = dedupe([*(existing.get("source_refs") or []), *(incoming.get("source_refs") or [])])
    evidence_ids = dedupe([*(existing.get("evidence_ids") or []), *(incoming.get("evidence_ids") or [])])
    atom_refs = dedupe([*(existing.get("atom_refs") or []), *(incoming.get("atom_refs") or [])])
    if incoming_status == "resolved" and incoming_resolution:
        merged.update(incoming)
        merged["gap_id"] = existing.get("gap_id") or incoming.get("gap_id")
        merged["created_at"] = existing.get("created_at") or incoming.get("created_at")
        merged["severity"] = strongest_severity
        merged["treatment_relevance"] = strongest_relevance
        merged["source_refs"] = source_refs
        merged["evidence_ids"] = evidence_ids
        merged["atom_refs"] = atom_refs
        if existing_question:
            merged["question"] = existing_question
        return merged

    merged.update(incoming)
    merged["gap_id"] = existing.get("gap_id") or incoming.get("gap_id")
    merged["created_at"] = existing.get("created_at") or incoming.get("created_at")
    merged["severity"] = strongest_severity
    merged["treatment_relevance"] = strongest_relevance
    merged["source_refs"] = source_refs
    merged["evidence_ids"] = evidence_ids
    merged["atom_refs"] = atom_refs
    if existing_question:
        merged["question"] = existing_question
    if existing.get("status") == "resolved" and incoming_status != "resolved":
        merged["status"] = "resolved"
        merged["resolution_note"] = existing.get("resolution_note") or incoming.get("resolution_note") or ""
    return merged


def gap_key(category: str, question: str) -> str:
    return f"{category}:{normalized_question_key(question)}"


def normalized_question_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(19|20)(\d{2})\s*-\s*(\d{2})\b", r"\1\2-\1\3", text)
    text = re.sub(r"\s+", " ", text)
    return text


def risk_from_gap(gap: dict[str, Any]) -> str:
    severity = str(gap.get("severity") or ResearchGapSeverity.NON_BLOCKING.value)
    category = str(gap.get("category") or ResearchGapCategory.OPTIONAL_CONTEXT.value)
    relevance = treatment_relevance(gap.get("treatment_relevance"))
    question = str(gap.get("question") or "").strip()
    note = str(gap.get("resolution_note") or "").strip()
    suffix = f" {note}" if note else ""
    return f"{severity} {relevance} research gap ({category}): {question}.{suffix}".strip()


def repair_atom_kinds_for_gap(gap: dict[str, Any]) -> list[str]:
    category = str(gap.get("category") or "")
    if category == "legal_detail":
        return ["mechanism"]
    if category == "chronology_detail":
        return ["context", "change"]
    if category == "contradiction":
        return ["tension"]
    if category == "weak_corroboration":
        return ["impact", "tension"]
    return ["open_question"]


def treatment_relevance(value: Any) -> str:
    text = str(value or "").strip()
    return text if text in RESEARCH_GAP_TREATMENT_RELEVANCE else ResearchGapTreatmentRelevance.SUPPORTING.value


__all__ = [
    "dedupe_gaps",
    "gap_key",
    "merge_research_gap",
    "normalized_question_key",
    "repair_atom_kinds_for_gap",
    "risk_from_gap",
    "treatment_relevance",
]
