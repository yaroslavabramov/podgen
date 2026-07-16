from __future__ import annotations

from typing import Any

from app.generation_v2.schemas import SourceMap
from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.source_triage_workspace_state import (
    dedupe as _dedupe,
    invalidate_coverage_syntheses,
    now as _now,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.generation_v2.tools.evidence_source_map_ids import public_gap_ref, source_map_record_fingerprint


_V2_WORKSPACE_SCHEMA = "source_triage_workspace.v2"
_REPLAN_REQUIRED_CODES = {
    "coverage_source_set_without_questions",
    "cyclic_coverage_question_hierarchy",
    "duplicate_coverage_questions",
    "duplicate_source_sets",
    "invalid_coverage_question_id",
    "invalid_central_coverage_question",
    "invalid_source_set_kind",
    "invalid_source_set_id",
    "missing_coverage_questions",
    "multiply_assigned_sources",
    "orientation_source_set_has_planned_questions",
    "unassigned_sources",
    "uninspected_orientation_sources",
    "unassigned_coverage_questions",
    "unknown_coverage_questions",
    "unknown_parent_coverage_questions",
    "unknown_set_coverage_questions",
    "unknown_source_assessments",
    "unknown_source_refs",
    "unexpected_coverage_outcomes",
}
_FINALIZER_ONLY_REPAIR_CODES = {
    "duplicate_coverage_syntheses",
    "invalid_coverage_synthesis",
    "missing_coverage_syntheses",
    "unknown_coverage_syntheses",
}


def create_repair_sets_from_gate(
    workspace: dict[str, Any],
    *,
    gate: Any,
    inventory: SourceMap,
    source_map: SourceMap | None = None,
) -> dict[str, Any]:
    if str(workspace.get("schema_version") or "") == _V2_WORKSPACE_SCHEMA:
        return _target_existing_v2_sets(
            workspace,
            gate=gate,
            inventory=inventory,
            source_map=source_map,
        )
    return _create_legacy_repair_set(
        workspace,
        gate=gate,
        inventory=inventory,
        source_map=source_map,
    )


def _target_existing_v2_sets(
    workspace: dict[str, Any],
    *,
    gate: Any,
    inventory: SourceMap,
    source_map: SourceMap | None,
) -> dict[str, Any]:
    source_sets = [dict(item) for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    sets_by_id = {str(item.get("set_id") or ""): item for item in source_sets if item.get("set_id")}
    set_ids_by_ref: dict[str, list[str]] = {}
    for source_set in source_sets:
        set_id = str(source_set.get("set_id") or "")
        for source_ref in source_set.get("source_refs") or []:
            set_ids_by_ref.setdefault(str(source_ref), []).append(set_id)

    targets: dict[str, dict[str, Any]] = {}
    unaddressable: list[dict[str, Any]] = []
    assessments_to_rebuild: set[str] = set()
    outcomes_to_rebuild: set[tuple[str, str]] = set()
    source_map_repair_items, source_map_repair_targets = _model_safe_source_map_repair_state(
        gate=gate,
        inventory=inventory,
        source_map=source_map,
    )
    if source_map_repair_items:
        workspace["source_map_repair_items"] = source_map_repair_items
        workspace["source_map_repair_targets"] = source_map_repair_targets
    else:
        workspace.pop("source_map_repair_items", None)
        workspace.pop("source_map_repair_targets", None)

    def target_set(
        set_id: str,
        *,
        code: str,
        message: str,
        source_refs: list[str] | None = None,
        question_ids: list[str] | None = None,
    ) -> None:
        source_set = sets_by_id.get(set_id)
        if source_set is None:
            unaddressable.append(_unaddressable(code, message, set_id=set_id))
            return
        set_refs = {str(item) for item in source_set.get("source_refs") or []}
        set_questions = {str(item) for item in source_set.get("question_ids") or []}
        target = targets.setdefault(
            set_id,
            {"source_refs": [], "question_ids": [], "issues": []},
        )
        target["source_refs"] = _dedupe(
            [
                *target["source_refs"],
                *[str(item) for item in source_refs or [] if str(item) in set_refs],
            ]
        )
        target["question_ids"] = _dedupe(
            [
                *target["question_ids"],
                *[str(item) for item in question_ids or [] if str(item) in set_questions],
            ]
        )
        issue = {
            "code": code,
            "message": message,
            "source_refs": _dedupe([str(item) for item in source_refs or [] if str(item) in set_refs]),
            "question_ids": _dedupe([str(item) for item in question_ids or [] if str(item) in set_questions]),
        }
        if issue not in target["issues"]:
            target["issues"].append(issue)

    def target_refs(*, code: str, message: str, source_refs: list[str]) -> None:
        for source_ref in _dedupe(source_refs):
            set_ids = _dedupe(set_ids_by_ref.get(source_ref, []))
            if len(set_ids) != 1:
                unaddressable.append(_unaddressable(code, message, source_refs=[source_ref]))
                continue
            target_set(
                set_ids[0],
                code=code,
                message=message,
                source_refs=[source_ref],
            )

    for violation in getattr(gate, "workspace_violations", []) or []:
        if not isinstance(violation, dict):
            unaddressable.append(_unaddressable("invalid_workspace_violation", str(violation)))
            continue
        code = str(violation.get("code") or "workspace_violation")
        message = str(violation.get("message") or code)
        if code in _FINALIZER_ONLY_REPAIR_CODES:
            # Per-set work is still valid. Rebuild only the authoritative
            # run-wide question synthesis in the normal finalizer repair pass.
            continue
        if code in _REPLAN_REQUIRED_CODES:
            unaddressable.append(
                _unaddressable(
                    code,
                    message,
                    source_refs=[str(item) for item in violation.get("source_refs") or []],
                    question_ids=[str(item) for item in violation.get("question_ids") or []],
                )
            )
            continue
        if code in {"inconsistent_source_set_status", "unfinished_source_set", "unexplained_deferred_source_set"}:
            target_set(str(violation.get("set_id") or ""), code=code, message=message)
            continue
        if code in {
            "deferred_set_assessment_mismatch",
            "deferred_source_index_mismatch",
            "duplicate_source_assessments",
            "inconsistent_source_assessment",
            "source_assessment_set_mismatch",
            "unassessed_sources",
            "unchecked_source_representations",
            "unfinished_source_assessments",
        }:
            source_refs = [str(item) for item in violation.get("source_refs") or []]
            if code in {
                "duplicate_source_assessments",
                "inconsistent_source_assessment",
                "source_assessment_set_mismatch",
            }:
                assessments_to_rebuild.update(source_refs)
            target_refs(code=code, message=message, source_refs=source_refs)
            continue
        if code in {
            "deferred_set_active_outcome",
            "deferred_source_active_outcome",
            "duplicate_coverage_outcomes",
            "ineligible_source_active_outcome",
            "incomplete_coverage_outcome",
            "unresolved_coverage_questions",
        }:
            for pair in violation.get("set_question_pairs") or []:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    unaddressable.append(_unaddressable(code, message))
                    continue
                set_id, question_id = str(pair[0]), str(pair[1])
                source_set = sets_by_id.get(set_id)
                if source_set is None or question_id not in {
                    str(item) for item in source_set.get("question_ids") or []
                }:
                    unaddressable.append(_unaddressable(code, message, set_id=set_id, question_ids=[question_id]))
                    continue
                if code in {
                    "deferred_set_active_outcome",
                    "deferred_source_active_outcome",
                    "duplicate_coverage_outcomes",
                    "ineligible_source_active_outcome",
                    "incomplete_coverage_outcome",
                }:
                    outcomes_to_rebuild.add((set_id, question_id))
                target_set(
                    set_id,
                    code=code,
                    message=message,
                    question_ids=[question_id],
                )
            continue
        unaddressable.append(_unaddressable(code, message))

    target_refs(
        code="unresolved_source_refs",
        message="Resolve the unfinished source state in its existing source set.",
        source_refs=[str(item) for item in getattr(gate, "unresolved_source_refs", []) or []],
    )

    if source_map is not None:
        invalid_evidence_source_ids = _source_ids_for_invalid_evidence(
            source_map,
            set(getattr(gate, "invalid_evidence_ids", []) or []),
        )
        context = build_source_ref_context(inventory)
        invalid_evidence_refs = [
            source_ref
            for source_id in invalid_evidence_source_ids
            if (source_ref := source_ref_for_id(source_id, context)) != "S??"
        ]
        target_refs(
            code="invalid_evidence",
            message="Replace invalid evidence references in the affected source assessment or outcome.",
            source_refs=invalid_evidence_refs,
        )

    for code, values in (
        ("unpersisted_completion_gap_notes", getattr(gate, "unpersisted_completion_gap_notes", []) or []),
    ):
        if values:
            unaddressable.append(
                {
                    "code": code,
                    "message": "This failure cannot be corrected by rerunning an existing source set.",
                    "count": len(values),
                }
            )

    if assessments_to_rebuild:
        baselines: list[dict[str, Any]] = []
        versions_by_ref: dict[str, int] = {}
        for raw in [
            *(workspace.get("repair_baseline_source_assessments") or []),
            *[
                item
                for item in workspace.get("source_assessments") or []
                if isinstance(item, dict)
                and str(item.get("source_ref") or "") in assessments_to_rebuild
            ],
        ]:
            if not isinstance(raw, dict):
                continue
            baseline = dict(raw)
            source_ref = str(baseline.get("source_ref") or "")
            if not source_ref:
                continue
            assigned_set_ids = _dedupe(set_ids_by_ref.get(source_ref, []))
            if len(assigned_set_ids) == 1:
                assigned_set_id = assigned_set_ids[0]
                stored_set_id = str(baseline.get("set_id") or "")
                if stored_set_id != assigned_set_id and "original_set_id" not in baseline:
                    baseline["original_set_id"] = stored_set_id or None
                baseline["set_id"] = assigned_set_id
            baseline["repair_baseline"] = True
            requested_version = _non_negative_int(baseline.get("repair_baseline_version"))
            next_version = max(versions_by_ref.get(source_ref, 0) + 1, requested_version)
            versions_by_ref[source_ref] = next_version
            baseline["repair_baseline_version"] = next_version
            baselines.append(baseline)
        workspace["repair_baseline_source_assessments"] = baselines
        workspace["source_assessments"] = [
            item
            for item in workspace.get("source_assessments") or []
            if not isinstance(item, dict) or str(item.get("source_ref") or "") not in assessments_to_rebuild
        ]
    if outcomes_to_rebuild:
        baselines = []
        versions_by_pair: dict[tuple[str, str], int] = {}
        for raw in [
            *(workspace.get("repair_baseline_coverage_outcomes") or []),
            *[
                item
                for item in workspace.get("coverage_outcomes") or []
                if isinstance(item, dict)
                and (str(item.get("set_id") or ""), str(item.get("question_id") or ""))
                in outcomes_to_rebuild
            ],
        ]:
            if not isinstance(raw, dict):
                continue
            baseline = dict(raw)
            pair = (
                str(baseline.get("set_id") or ""),
                str(baseline.get("question_id") or ""),
            )
            if not all(pair):
                continue
            baseline["repair_baseline"] = True
            requested_version = _non_negative_int(baseline.get("repair_baseline_version"))
            next_version = max(versions_by_pair.get(pair, 0) + 1, requested_version)
            versions_by_pair[pair] = next_version
            baseline["repair_baseline_version"] = next_version
            baselines.append(baseline)
        workspace["repair_baseline_coverage_outcomes"] = baselines
        workspace["coverage_outcomes"] = [
            item
            for item in workspace.get("coverage_outcomes") or []
            if not isinstance(item, dict)
            or (
                str(item.get("set_id") or ""),
                str(item.get("question_id") or ""),
            )
            not in outcomes_to_rebuild
        ]

    updated_sets: list[dict[str, Any]] = []
    for source_set in source_sets:
        set_id = str(source_set.get("set_id") or "")
        target = targets.get(set_id)
        if target is not None:
            source_set["status"] = "planned"
            source_set["repair"] = True
            source_set["repair_targeted"] = True
            source_set["repair_source_refs"] = target["source_refs"]
            source_set["repair_question_ids"] = target["question_ids"]
            source_set["repair_targets"] = target["issues"]
            source_set["resume_reason"] = "Targeted backend validation repair."
            source_set["updated_at"] = _now()
        updated_sets.append(source_set)
    workspace["source_sets"] = updated_sets
    # Any repaired assessment or per-set outcome can change the run-wide
    # answer. The finalizer must synthesize it again from the repaired state.
    invalidate_coverage_syntheses(workspace)
    workspace["repair_unaddressable_violations"] = _dedupe_issue_dicts(unaddressable)
    return workspace


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(str(value or "0")))
    except ValueError:
        return 0


def _unaddressable(
    code: str,
    message: str,
    *,
    set_id: str = "",
    source_refs: list[str] | None = None,
    question_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "set_id": set_id,
        "source_refs": _dedupe(source_refs or []),
        "question_ids": _dedupe(question_ids or []),
    }


def _dedupe_issue_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        key = (
            str(item.get("code") or ""),
            str(item.get("message") or ""),
            str(item.get("set_id") or ""),
            tuple(str(value) for value in item.get("source_refs") or []),
            tuple(str(value) for value in item.get("question_ids") or []),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _create_legacy_repair_set(
    workspace: dict[str, Any],
    *,
    gate: Any,
    inventory: SourceMap,
    source_map: SourceMap | None,
) -> dict[str, Any]:
    context = build_source_ref_context(inventory)
    safe_gate_summary = model_preview_text(
        getattr(gate, "summary", "Repair source triage gate issues."),
        replacements=context.ref_by_source_id,
        collapse_whitespace=False,
    )
    source_ids: set[str] = set()
    for key in (
        "missing_source_card_evidence_source_ids",
        "missing_model_read_source_ids",
        "missing_source_card_write_source_ids",
        "invalid_source_ids",
    ):
        source_ids.update(str(item) for item in getattr(gate, key, []) or [])
    if source_map is not None:
        source_ids.update(
            _source_ids_for_invalid_evidence(source_map, set(getattr(gate, "invalid_evidence_ids", []) or []))
        )
        source_ids.update(
            _source_ids_for_ungrounded_items(source_map, getattr(gate, "ungrounded_relationships", []) or [])
        )
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
    refs = _dedupe(
        [*refs, *[str(item) for item in getattr(gate, "unresolved_source_refs", []) or [] if str(item) in source_order]]
    )
    if not refs and "finalize" not in str(getattr(gate, "summary", "")).lower():
        refs = [str(ref) for ref in (workspace.get("mapped_sources") or workspace.get("source_order") or [])[:6]]
    if not refs:
        return workspace
    repair_idx = 1 + len(
        [item for item in workspace.get("source_sets") or [] if str(item.get("set_id", "")).startswith("repair_")]
    )
    repair_set = {
        "set_id": f"repair_{repair_idx}",
        "set_kind": "coverage",
        "label": f"Repair source triage gate issues {repair_idx}",
        "purpose": safe_gate_summary,
        "source_refs": refs,
        "status": "planned",
        "rationale": "Created from source triage gate failure.",
        "questions": _gate_repair_questions(gate, summary=safe_gate_summary),
        "priority": "repair",
        "findings_summary": "",
        "evidence_ids": [],
        "written_actions": [],
        "observations": [],
        "deferred_reason": "",
        "repair": True,
        "repair_targeted": True,
        "updated_at": _now(),
    }
    workspace["source_sets"] = [*(workspace.get("source_sets") or []), repair_set]
    return workspace


def _gate_repair_questions(gate: Any, *, summary: str | None = None) -> list[str]:
    questions = [summary if summary is not None else str(getattr(gate, "summary", ""))]
    for label, values in (
        ("invalid evidence", getattr(gate, "invalid_evidence_ids", []) or []),
        ("ungrounded relationship/cluster", getattr(gate, "ungrounded_relationships", []) or []),
        ("unpersisted gap note", getattr(gate, "unpersisted_completion_gap_notes", []) or []),
        ("missing function evidence", getattr(gate, "missing_function_evidence", []) or []),
    ):
        if values:
            questions.append(f"Repair {label} ({len(values)} affected item(s)).")
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
    return source_ids


def _source_ids_for_ungrounded_items(source_map: SourceMap, ungrounded_items: list[str]) -> set[str]:
    source_ids: set[str] = set()
    clusters_by_id = {cluster.cluster_id: cluster for cluster in source_map.source_triage.source_clusters}
    gaps_by_id = {gap.gap_id: gap for gap in source_map.gap_records}
    for item in ungrounded_items:
        text = str(item or "")
        parts = text.split(":")
        if text.startswith("cluster:"):
            cluster = clusters_by_id.get(parts[-1])
            if cluster:
                source_ids.update(cluster.source_ids)
            continue
        if text.startswith("gap:"):
            gap = gaps_by_id.get(":".join(parts[2:]) if len(parts) > 3 else parts[-1])
            if gap:
                source_ids.update(gap.source_ids)
            continue
        if parts[:2] == ["relationship", "self"] and len(parts) >= 4:
            source_ids.add(parts[2])
            continue
        if parts[:2] == ["relationship", "evidence_scope"] and len(parts) >= 5:
            source_ids.update({parts[2], parts[-1]})
            continue
        if len(parts) >= 3:
            source_ids.update({parts[0], parts[-1]})
    return source_ids


def _model_safe_source_map_repair_state(
    *,
    gate: Any,
    inventory: SourceMap,
    source_map: SourceMap | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if source_map is None:
        return [], {}
    context = build_source_ref_context(inventory)
    relationships_by_key = {
        (item.source_id, str(item.relationship), item.related_source_id): item
        for item in source_map.source_triage.source_relationships
    }
    clusters_by_id = {cluster.cluster_id: cluster for cluster in source_map.source_triage.source_clusters}
    gaps_by_id = {gap.gap_id: gap for gap in source_map.gap_records}
    issue_names = {
        "self": "self_relationship",
        "evidence_scope": "evidence_outside_declared_sources",
        "membership": "insufficient_cluster_membership",
        "role_scope": "role_outside_cluster",
        "role_overlap": "overlapping_cluster_roles",
    }
    entries: dict[tuple[str, ...], dict[str, Any]] = {}

    def source_refs(source_ids: list[str]) -> list[str]:
        return _dedupe(
            [
                source_ref
                for source_id in source_ids
                if (source_ref := source_ref_for_id(source_id, context)) != "S??"
            ]
        )

    def safe_text(value: Any) -> str:
        return model_preview_text(
            value,
            replacements=context.ref_by_source_id,
            collapse_whitespace=False,
        )

    def add_relationship(item: Any, issue: str) -> None:
        key = ("relationship", item.source_id, str(item.relationship), item.related_source_id)
        entry = entries.setdefault(
            key,
            {
                "kind": "relationship",
                "issues": [],
                "source_refs": source_refs([item.source_id, item.related_source_id]),
                "relationship": str(item.relationship),
                "evidence_ids": list(item.evidence_ids),
                "rationale": safe_text(item.rationale),
                "target": {
                    "target_kind": "relationship",
                    "source_id": item.source_id,
                    "related_source_id": item.related_source_id,
                    "relationship": str(item.relationship),
                    "target_fingerprint": source_map_record_fingerprint("relationship", item),
                },
            },
        )
        entry["issues"] = _dedupe([*entry["issues"], issue])

    def add_cluster(item: Any, issue: str) -> None:
        key = ("cluster", item.cluster_id)
        entry = entries.setdefault(
            key,
            {
                "kind": "cluster",
                "issues": [],
                "cluster_id": item.cluster_id,
                "label": safe_text(item.label),
                "source_refs": source_refs(list(item.source_ids)),
                "primary_source_refs": source_refs(list(item.primary_source_ids)),
                "secondary_source_refs": source_refs(list(item.secondary_source_ids)),
                "duplicate_source_refs": source_refs(list(item.duplicate_source_ids)),
                "evidence_ids": list(item.evidence_ids),
                "rationale": safe_text(item.rationale),
                "target": {
                    "target_kind": "cluster",
                    "cluster_id": item.cluster_id,
                    "target_fingerprint": source_map_record_fingerprint("cluster", item),
                },
            },
        )
        entry["issues"] = _dedupe([*entry["issues"], issue])

    def add_gap(item: Any, issue: str) -> None:
        key = ("gap", item.gap_id)
        entry = entries.setdefault(
            key,
            {
                "kind": "gap",
                "issues": [],
                "gap_ref": public_gap_ref(item.gap_id),
                "gap_kind": str(item.kind or "gap"),
                "description": safe_text(item.description),
                "source_refs": source_refs(list(item.source_ids)),
                "question_ids": list(item.question_ids),
                "evidence_ids": list(item.evidence_ids),
                "target": {
                    "target_kind": "gap",
                    "gap_id": item.gap_id,
                    "target_fingerprint": source_map_record_fingerprint("gap", item),
                },
            },
        )
        entry["issues"] = _dedupe([*entry["issues"], issue])

    for raw_item in getattr(gate, "ungrounded_relationships", []) or []:
        parts = str(raw_item or "").split(":")
        if parts[:1] == ["relationship"] and len(parts) >= 4:
            relation_key = (
                parts[2],
                parts[-2] if parts[1] == "evidence_scope" else parts[-1],
                parts[-1] if parts[1] == "evidence_scope" else parts[2],
            )
            if relation := relationships_by_key.get(relation_key):
                add_relationship(relation, issue_names.get(parts[1], "invalid_structural_grounding"))
        elif parts[:1] == ["cluster"] and len(parts) >= 2:
            if cluster := clusters_by_id.get(parts[-1]):
                add_cluster(cluster, issue_names.get(parts[1], "missing_structural_evidence"))
        elif parts[:2] == ["gap", "evidence_scope"] and len(parts) >= 3:
            if gap := gaps_by_id.get(":".join(parts[2:])):
                add_gap(gap, "evidence_outside_declared_sources")
        elif len(parts) >= 3:
            if relation := relationships_by_key.get((parts[0], parts[-2], parts[-1])):
                add_relationship(relation, "missing_structural_evidence")

    invalid_evidence_ids = set(getattr(gate, "invalid_evidence_ids", []) or [])
    for relation in source_map.source_triage.source_relationships:
        if invalid_evidence_ids.intersection(relation.evidence_ids):
            add_relationship(relation, "invalid_evidence_reference")
    for cluster in source_map.source_triage.source_clusters:
        if invalid_evidence_ids.intersection(cluster.evidence_ids):
            add_cluster(cluster, "invalid_evidence_reference")
    for gap in source_map.gap_records:
        if invalid_evidence_ids.intersection(gap.evidence_ids):
            add_gap(gap, "invalid_evidence_reference")

    items: list[dict[str, Any]] = []
    targets: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries.values(), start=1):
        repair_ref = f"SMR{index:02d}"
        target = dict(entry.pop("target"))
        items.append({"repair_ref": repair_ref, "status": "open", **entry})
        targets[repair_ref] = target
    return items, targets


def _source_order(inventory: SourceMap) -> list[str]:
    context = build_source_ref_context(inventory)
    return [source_ref_for_id(card.source_id, context) for card in inventory.source_cards]
