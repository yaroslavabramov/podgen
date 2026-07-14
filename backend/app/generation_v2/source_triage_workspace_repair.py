from __future__ import annotations

from typing import Any

from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_state import dedupe as _dedupe, now as _now
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


def create_repair_sets_from_gate(
    workspace: dict[str, Any],
    *,
    gate: Any,
    inventory: SourceMap,
    source_map: SourceMap | None = None,
) -> dict[str, Any]:
    context = build_source_ref_context(inventory)
    source_ids: set[str] = set()
    for key in (
        "missing_source_card_evidence_source_ids",
        "missing_model_read_source_ids",
        "missing_source_card_write_source_ids",
        "invalid_source_ids",
    ):
        source_ids.update(str(item) for item in getattr(gate, key, []) or [])
    if source_map is not None:
        source_ids.update(_source_ids_for_invalid_evidence(source_map, set(getattr(gate, "invalid_evidence_ids", []) or [])))
        source_ids.update(_source_ids_for_ungrounded_items(source_map, getattr(gate, "ungrounded_relationships", []) or []))
        if getattr(gate, "unpersisted_completion_gap_notes", None):
            source_ids.update(card.source_id for card in source_map.source_cards[:6])
        if getattr(gate, "checked_evidence_count", 0) == 0:
            source_ids.update(card.source_id for card in source_map.source_cards[:6])
    source_order = {str(ref) for ref in workspace.get("source_order") or _source_order(inventory)}
    refs = _dedupe(
        [
            ref
            for source_id in source_ids
            if source_id and (ref := source_ref_for_id(source_id, context)) in source_order
        ]
    )
    refs = _dedupe([*refs, *[str(item) for item in getattr(gate, "unresolved_source_refs", []) or [] if str(item) in source_order]])
    if not refs and "finalize" not in str(getattr(gate, "summary", "")).lower():
        refs = [str(ref) for ref in (workspace.get("mapped_sources") or workspace.get("source_order") or [])[:6]]
    if not refs:
        return workspace
    repair_idx = 1 + len([item for item in workspace.get("source_sets") or [] if str(item.get("set_id", "")).startswith("repair_")])
    repair_set = {
        "set_id": f"repair_{repair_idx}",
        "label": f"Repair source triage gate issues {repair_idx}",
        "purpose": str(getattr(gate, "summary", "Repair source triage gate issues.")),
        "source_refs": refs,
        "status": "planned",
        "rationale": "Created from source triage gate failure.",
        "questions": _gate_repair_questions(gate),
        "priority": "repair",
        "findings_summary": "",
        "evidence_ids": [],
        "written_actions": [],
        "observations": [],
        "deferred_reason": "",
        "repair": True,
        "updated_at": _now(),
    }
    workspace["source_sets"] = [*(workspace.get("source_sets") or []), repair_set]
    return workspace


def _gate_repair_questions(gate: Any) -> list[str]:
    questions = [str(getattr(gate, "summary", ""))]
    for label, values in (
        ("invalid evidence", getattr(gate, "invalid_evidence_ids", []) or []),
        ("ungrounded relationship/cluster", getattr(gate, "ungrounded_relationships", []) or []),
        ("unpersisted gap note", getattr(gate, "unpersisted_completion_gap_notes", []) or []),
        ("missing function evidence", getattr(gate, "missing_function_evidence", []) or []),
    ):
        if values:
            questions.append(f"Repair {label}: {', '.join(str(item) for item in values)}")
    return [question for question in questions if question]


def _source_ids_for_invalid_evidence(source_map: SourceMap, invalid_evidence_ids: set[str]) -> set[str]:
    if not invalid_evidence_ids:
        return set()
    source_ids: set[str] = set()
    for card in source_map.source_cards:
        if invalid_evidence_ids.intersection(card.evidence_ids):
            source_ids.add(card.source_id)
    for quote in source_map.quote_cards:
        if quote.evidence_id in invalid_evidence_ids:
            source_ids.add(quote.source_id)
    for cluster in source_map.source_triage.source_clusters:
        if invalid_evidence_ids.intersection(cluster.evidence_ids):
            source_ids.update(cluster.source_ids)
    for relation in source_map.source_triage.source_relationships:
        if invalid_evidence_ids.intersection(relation.evidence_ids):
            source_ids.add(relation.source_id)
            source_ids.add(relation.related_source_id)
    return source_ids


def _source_ids_for_ungrounded_items(source_map: SourceMap, ungrounded_items: list[str]) -> set[str]:
    source_ids: set[str] = set()
    clusters_by_id = {cluster.cluster_id: cluster for cluster in source_map.source_triage.source_clusters}
    for item in ungrounded_items:
        text = str(item or "")
        if text.startswith("cluster:"):
            cluster = clusters_by_id.get(text.split(":", 1)[1])
            if cluster:
                source_ids.update(cluster.source_ids)
            continue
        parts = text.split(":")
        if parts and parts[0]:
            source_ids.add(parts[0])
        if len(parts) >= 3 and parts[-1]:
            source_ids.add(parts[-1])
    return source_ids


def _source_order(inventory: SourceMap) -> list[str]:
    context = build_source_ref_context(inventory)
    return [source_ref_for_id(card.source_id, context) for card in inventory.source_cards]
