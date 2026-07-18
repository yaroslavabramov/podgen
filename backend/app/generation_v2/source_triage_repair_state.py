from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


TRIAGE_REPAIR_RESERVED = "reserved"
TRIAGE_REPAIR_IN_PROGRESS = "in_progress"
TRIAGE_REPAIR_COMPLETED = "completed"
TRIAGE_REPAIR_PENDING = frozenset({TRIAGE_REPAIR_RESERVED, TRIAGE_REPAIR_IN_PROGRESS})


def triage_repair_operation(workspace: dict[str, Any]) -> dict[str, Any] | None:
    operation = workspace.get("repair_operation")
    if not isinstance(operation, dict):
        return None
    if operation.get("status") not in {*TRIAGE_REPAIR_PENDING, TRIAGE_REPAIR_COMPLETED}:
        return None
    return deepcopy(operation)


def pending_triage_repair(workspace: dict[str, Any]) -> dict[str, Any] | None:
    operation = triage_repair_operation(workspace)
    if operation is None or operation.get("status") not in TRIAGE_REPAIR_PENDING:
        return None
    return operation


def reserve_triage_repair(
    workspace: dict[str, Any],
    *,
    gate_payload: dict[str, Any],
    completion_payload: dict[str, Any],
    repair_sets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    existing = triage_repair_operation(workspace)
    if existing is not None:
        return existing if existing.get("status") in TRIAGE_REPAIR_PENDING else None
    now = _now()
    operation = {
        "status": TRIAGE_REPAIR_RESERVED,
        "payload": {
            "gate": deepcopy(gate_payload),
            "completion": deepcopy(completion_payload),
            "repair_sets": deepcopy(repair_sets),
            "repair_set_ids": [str(item.get("set_id") or "") for item in repair_sets if item.get("set_id")],
        },
        "reserved_at": now,
        "updated_at": now,
    }
    workspace["repair_operation"] = operation
    workspace["status"] = "repair_planned"
    return deepcopy(operation)


def start_triage_repair(workspace: dict[str, Any]) -> dict[str, Any] | None:
    operation = pending_triage_repair(workspace)
    if operation is None:
        return None
    now = _now()
    operation["status"] = TRIAGE_REPAIR_IN_PROGRESS
    operation.setdefault("started_at", now)
    operation["updated_at"] = now
    workspace["repair_operation"] = operation
    workspace["status"] = "repair_in_progress"
    return deepcopy(operation)


def complete_triage_repair(workspace: dict[str, Any]) -> bool:
    operation = triage_repair_operation(workspace)
    if operation is None:
        return False
    if operation.get("status") == TRIAGE_REPAIR_COMPLETED:
        return True
    if operation.get("status") not in TRIAGE_REPAIR_PENDING:
        return False
    now = _now()
    operation["status"] = TRIAGE_REPAIR_COMPLETED
    operation["completed_at"] = now
    operation["updated_at"] = now
    workspace["repair_operation"] = operation
    return True


def prepare_pending_triage_repair(workspace: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(workspace)
    operation = pending_triage_repair(current)
    if operation is None:
        return current
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    repair_set_id_list = [str(item) for item in payload.get("repair_set_ids") or [] if str(item).strip()]
    repair_set_ids = set(repair_set_id_list)
    existing_by_id = {
        str(item.get("set_id") or ""): deepcopy(item)
        for item in current.get("source_sets") or []
        if isinstance(item, dict) and item.get("set_id")
    }
    for item in payload.get("repair_sets") or []:
        if isinstance(item, dict) and item.get("set_id"):
            existing_by_id.setdefault(str(item["set_id"]), deepcopy(item))
    source_sets: list[dict[str, Any]] = []
    for item in current.get("source_sets") or []:
        if not isinstance(item, dict):
            continue
        set_id = str(item.get("set_id") or "")
        source_set = deepcopy(item)
        if set_id in repair_set_ids and source_set.get("status") == "in_progress":
            source_set["status"] = "planned"
            source_set["resume_reason"] = "Targeted triage repair stopped before durable completion."
        source_sets.append(source_set)
    present = {str(item.get("set_id") or "") for item in source_sets}
    source_sets.extend(
        existing_by_id[set_id] for set_id in repair_set_id_list if set_id not in present and set_id in existing_by_id
    )
    current["source_sets"] = source_sets
    current["current_set"] = None
    current["status"] = "repair_planned"
    return current


def carry_pending_triage_repair(
    existing: dict[str, Any],
    workspace: dict[str, Any],
    *,
    source_order: list[str],
) -> dict[str, Any]:
    operation = pending_triage_repair(existing)
    if operation is None:
        return workspace
    if int(existing.get("corpus_revision") or 1) != int(workspace.get("corpus_revision") or 1):
        # A repair is scoped to the corpus revision that produced it. A new
        # revision needs a new plan, not a repair of stale source assignments.
        return workspace
    existing_source_order = [str(item) for item in existing.get("source_order") or []]
    if existing_source_order and existing_source_order != source_order:
        # Source refs are positional opaque handles. Reusing them after an
        # order change could attach an assessment to a different source.
        return workspace

    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    raw_repair_sets = payload.get("repair_sets") or [
        item for item in existing.get("source_sets") or [] if isinstance(item, dict) and item.get("repair")
    ]
    repair_set_id_list: list[str] = []
    for set_id in [
        *[str(item) for item in payload.get("repair_set_ids") or []],
        *[str(item.get("set_id") or "") for item in raw_repair_sets if isinstance(item, dict)],
    ]:
        if set_id.strip() and set_id not in repair_set_id_list:
            repair_set_id_list.append(set_id)
    repair_set_ids = set(repair_set_id_list)
    repair_sets_by_id = {
        str(item.get("set_id") or ""): deepcopy(item)
        for item in raw_repair_sets
        if isinstance(item, dict) and item.get("set_id")
    }
    existing_sets_by_id = {
        str(item.get("set_id") or ""): deepcopy(item)
        for item in existing.get("source_sets") or []
        if isinstance(item, dict) and item.get("set_id")
    }

    carried_sets: list[dict[str, Any]] = []
    seen_set_ids: set[str] = set()
    for item in existing.get("source_sets") or []:
        if not isinstance(item, dict) or not item.get("set_id"):
            continue
        set_id = str(item["set_id"])
        source_set = deepcopy(item)
        if set_id in repair_set_ids:
            # The top-level workspace is newer than the reservation payload
            # when a repair already started. Preserve that durable work.
            source_set = _repair_set_for_new_attempt(source_set, source_order=source_order)
        carried_sets.append(source_set)
        seen_set_ids.add(set_id)
    for set_id in repair_set_id_list:
        if set_id in seen_set_ids:
            continue
        source_set = existing_sets_by_id.get(set_id) or repair_sets_by_id.get(set_id)
        if source_set is None:
            continue
        carried_sets.append(_repair_set_for_new_attempt(source_set, source_order=source_order))
        seen_set_ids.add(set_id)

    repair_sets = [deepcopy(item) for item in carried_sets if str(item.get("set_id") or "") in repair_set_ids]
    carried_payload = dict(payload)
    carried_payload["repair_sets"] = deepcopy(repair_sets)
    carried_payload["repair_set_ids"] = [str(item.get("set_id") or "") for item in repair_sets if item.get("set_id")]
    operation["payload"] = carried_payload

    # Keep the complete canonical context. Only the targeted sets are reopened;
    # unaffected assessments and question outcomes remain authoritative.
    for field in (
        "intent",
        "source_capsules",
        "coverage_questions",
        "source_assessments",
        "source_findings",
        "coverage_outcomes",
        "triage_semantic_revision",
        "repair_baseline_source_assessments",
        "repair_baseline_coverage_outcomes",
        "deferred_sources",
        "mapped_sources",
        "open_questions",
        "plan_summary",
        "source_map_repair_items",
        "source_map_repair_targets",
    ):
        if field in existing:
            workspace[field] = deepcopy(existing[field])
    workspace["source_sets"] = carried_sets
    workspace["processed_sets"] = [
        deepcopy(item)
        for item in existing.get("processed_sets") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") not in repair_set_ids
    ]
    workspace["repair_operation"] = operation
    workspace["current_set"] = None
    # Finalization and materialized gap traces belong to the previous attempt.
    workspace.pop("source_map_finalization", None)
    workspace.pop("materialized_deferred_source_refs", None)
    workspace["status"] = "repair_planned"
    return workspace


def triage_workspace_evidence_ids(workspace: dict[str, Any]) -> list[str]:
    """Return evidence needed to restore current and pre-repair semantic context."""
    values: list[str] = []
    for field in (
        "source_assessments",
        "source_findings",
        "coverage_outcomes",
        "repair_baseline_source_assessments",
        "repair_baseline_coverage_outcomes",
        "source_sets",
        "processed_sets",
    ):
        values.extend(_nested_evidence_ids(workspace.get(field)))
    return _dedupe(values)


def reconcile_carried_triage_evidence(
    workspace: dict[str, Any],
    *,
    evidence_id_map: dict[str, str],
    invalid_evidence_ids: set[str],
) -> dict[str, Any]:
    """Remap immutable evidence and reopen semantic records whose evidence cannot be carried."""
    current = deepcopy(workspace)
    invalid = {str(item) for item in invalid_evidence_ids if str(item).strip()}
    mapping = {str(old): str(new) for old, new in evidence_id_map.items() if str(old).strip() and str(new).strip()}
    affected_sets: dict[str, dict[str, set[str]]] = {}
    assessment_refs: set[str] = set()

    def affect(
        set_id: str,
        *,
        source_refs: list[str] | None = None,
        question_ids: list[str] | None = None,
    ) -> None:
        if not set_id:
            return
        target = affected_sets.setdefault(set_id, {"source_refs": set(), "question_ids": set()})
        target["source_refs"].update(str(item) for item in source_refs or [] if str(item).strip())
        target["question_ids"].update(str(item) for item in question_ids or [] if str(item).strip())

    findings: list[dict[str, Any]] = []
    stale_finding_source_refs: set[str] = set()
    for raw in current.get("source_findings") or []:
        if not isinstance(raw, dict):
            continue
        stale_ids = invalid.intersection(_nested_evidence_ids(raw))
        if stale_ids:
            source_ref = str(raw.get("source_ref") or "")
            stale_finding_source_refs.add(source_ref)
            assessment_refs.add(source_ref)
            affect(
                str(raw.get("set_id") or ""),
                source_refs=[source_ref],
                question_ids=[
                    str(link.get("question_id") or "")
                    for link in raw.get("question_links") or []
                    if isinstance(link, dict)
                ],
            )
            continue
        findings.append(_remap_nested_evidence(raw, mapping, invalid))
    current["source_findings"] = findings
    valid_finding_ids = {
        str(item.get("finding_id") or "")
        for item in findings
        if str(item.get("finding_id") or "")
    }

    assessments: list[dict[str, Any]] = []
    for raw in current.get("source_assessments") or []:
        if not isinstance(raw, dict):
            continue
        stale_ids = invalid.intersection(_nested_evidence_ids(raw))
        source_ref = str(raw.get("source_ref") or "")
        if stale_ids or source_ref in stale_finding_source_refs:
            assessment_refs.add(source_ref)
            affect(str(raw.get("set_id") or ""), source_refs=[source_ref])
            continue
        assessment = _remap_nested_evidence(raw, mapping, invalid)
        assessment["finding_ids"] = [
            str(finding_id)
            for finding_id in assessment.get("finding_ids") or []
            if str(finding_id) in valid_finding_ids
        ]
        assessments.append(assessment)
    current["source_assessments"] = assessments

    outcomes: list[dict[str, Any]] = []
    for raw in current.get("coverage_outcomes") or []:
        if not isinstance(raw, dict):
            continue
        stale_ids = invalid.intersection(_nested_evidence_ids(raw))
        raw_finding_ids = {
            str(finding_id) for finding_id in raw.get("finding_ids") or [] if str(finding_id)
        }
        if stale_ids or bool(raw_finding_ids - valid_finding_ids):
            affect(
                str(raw.get("set_id") or ""),
                source_refs=[str(item) for item in raw.get("source_refs") or []],
                question_ids=[str(raw.get("question_id") or "")],
            )
            continue
        outcomes.append(_remap_nested_evidence(raw, mapping, invalid))
    current["coverage_outcomes"] = outcomes

    baseline_assessments: list[dict[str, Any]] = []
    for raw in current.get("repair_baseline_source_assessments") or []:
        if not isinstance(raw, dict):
            continue
        stale_ids = invalid.intersection(_nested_evidence_ids(raw))
        baseline = _remap_nested_evidence(raw, mapping, invalid)
        if stale_ids:
            source_ref = str(raw.get("source_ref") or "")
            assessment_refs.add(source_ref)
            affect(str(raw.get("set_id") or ""), source_refs=[source_ref])
            baseline["repair_baseline_evidence_unavailable"] = True
            baseline["repair_baseline_unavailable_evidence_count"] = len(stale_ids)
        baseline_assessments.append(baseline)
    if "repair_baseline_source_assessments" in current:
        current["repair_baseline_source_assessments"] = baseline_assessments

    baseline_outcomes: list[dict[str, Any]] = []
    for raw in current.get("repair_baseline_coverage_outcomes") or []:
        if not isinstance(raw, dict):
            continue
        stale_ids = invalid.intersection(_nested_evidence_ids(raw))
        baseline = _remap_nested_evidence(raw, mapping, invalid)
        if stale_ids:
            affect(
                str(raw.get("set_id") or ""),
                source_refs=[str(item) for item in raw.get("source_refs") or []],
                question_ids=[str(raw.get("question_id") or "")],
            )
            baseline["repair_baseline_evidence_unavailable"] = True
            baseline["repair_baseline_unavailable_evidence_count"] = len(stale_ids)
        baseline_outcomes.append(baseline)
    if "repair_baseline_coverage_outcomes" in current:
        current["repair_baseline_coverage_outcomes"] = baseline_outcomes

    source_sets: list[dict[str, Any]] = []
    for raw in current.get("source_sets") or []:
        if not isinstance(raw, dict):
            continue
        source_set = deepcopy(raw)
        set_id = str(source_set.get("set_id") or "")
        stale_set_ids = invalid.intersection(_nested_evidence_ids(source_set))
        for field in ("observations", "written_actions"):
            retained: list[Any] = []
            for item in source_set.get(field) or []:
                if isinstance(item, dict) and invalid.intersection(_nested_evidence_ids(item)):
                    affect(
                        set_id,
                        source_refs=[str(ref) for ref in item.get("source_refs") or []],
                    )
                    continue
                retained.append(_remap_nested_evidence(item, mapping, invalid))
            source_set[field] = retained
        source_set = _remap_nested_evidence(source_set, mapping, invalid)
        target = affected_sets.get(set_id)
        if stale_set_ids and target is None:
            # Aggregate-only evidence cannot be attributed more narrowly.
            affect(
                set_id,
                source_refs=[str(item) for item in source_set.get("source_refs") or []],
                question_ids=[str(item) for item in source_set.get("question_ids") or []],
            )
        elif stale_set_ids and target is not None and not target["source_refs"] and not target["question_ids"]:
            # A nested record without ownership metadata still belongs to this
            # set; reopen its assigned sources instead of inventing attribution.
            affect(
                set_id,
                source_refs=[str(item) for item in source_set.get("source_refs") or []],
            )
        target = affected_sets.get(set_id)
        if target is not None:
            source_set["status"] = "planned"
            source_set["repair"] = True
            source_set["repair_targeted"] = True
            source_set["repair_source_refs"] = _dedupe(
                [
                    *[str(item) for item in source_set.get("repair_source_refs") or []],
                    *sorted(target["source_refs"]),
                ]
            )
            source_set["repair_question_ids"] = _dedupe(
                [
                    *[str(item) for item in source_set.get("repair_question_ids") or []],
                    *sorted(target["question_ids"]),
                ]
            )
            repair_target = {
                "code": "carried_evidence_unavailable",
                "message": "Re-read this item because its prior evidence is not valid in the current corpus attempt.",
                "source_refs": sorted(target["source_refs"]),
                "question_ids": sorted(target["question_ids"]),
            }
            targets = [dict(item) for item in source_set.get("repair_targets") or [] if isinstance(item, dict)]
            if repair_target not in targets:
                targets.append(repair_target)
            source_set["repair_targets"] = targets
            source_set["resume_reason"] = "Prior evidence could not be safely carried into this attempt."
            source_set["updated_at"] = _now()
            source_set.pop("started_at", None)
        source_sets.append(source_set)
    current["source_sets"] = source_sets

    _recompute_set_evidence(current)
    affected_set_ids = set(affected_sets)
    current["processed_sets"] = [
        _remap_nested_evidence(item, mapping, invalid)
        for item in current.get("processed_sets") or []
        if not isinstance(item, dict) or str(item.get("set_id") or "") not in affected_set_ids
    ]
    if assessment_refs:
        current["mapped_sources"] = [
            str(item) for item in current.get("mapped_sources") or [] if str(item) not in assessment_refs
        ]
        current["deferred_sources"] = [
            item
            for item in current.get("deferred_sources") or []
            if not isinstance(item, dict) or str(item.get("source_ref") or "") not in assessment_refs
        ]

    operation = current.get("repair_operation")
    if isinstance(operation, dict):
        payload = dict(operation.get("payload") or {})
        repair_ids = _dedupe(
            [
                *[str(item) for item in payload.get("repair_set_ids") or []],
                *[
                    str(item.get("set_id") or "")
                    for item in source_sets
                    if str(item.get("set_id") or "") in affected_set_ids
                ],
            ]
        )
        sets_by_id = {str(item.get("set_id") or ""): item for item in source_sets}
        payload["repair_set_ids"] = [set_id for set_id in repair_ids if set_id in sets_by_id]
        payload["repair_sets"] = [deepcopy(sets_by_id[set_id]) for set_id in payload["repair_set_ids"]]
        operation["payload"] = payload
        operation["updated_at"] = _now()
        current["repair_operation"] = operation
    return current


def _repair_set_for_new_attempt(source_set: dict[str, Any], *, source_order: list[str]) -> dict[str, Any]:
    current = deepcopy(source_set)
    valid_refs = set(source_order)
    current["source_refs"] = [str(ref) for ref in current.get("source_refs") or [] if str(ref) in valid_refs]
    current.update(
        {
            "status": "planned",
            "repair": True,
            "resume_reason": "Targeted triage repair resumed in a new corpus attempt.",
            "updated_at": _now(),
        }
    )
    current.pop("started_at", None)
    return current


def _nested_evidence_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                result.extend(str(evidence_id) for evidence_id in item if str(evidence_id).strip())
            else:
                result.extend(_nested_evidence_ids(item))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_nested_evidence_ids(item))
        return result
    return []


def _remap_nested_evidence(value: Any, mapping: dict[str, str], invalid: set[str]) -> Any:
    if isinstance(value, dict):
        remapped: dict[str, Any] = {}
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                remapped[key] = _dedupe(
                    [
                        mapping.get(str(evidence_id), str(evidence_id))
                        for evidence_id in item
                        if str(evidence_id) not in invalid
                    ]
                )
            else:
                remapped[key] = _remap_nested_evidence(item, mapping, invalid)
        return remapped
    if isinstance(value, list):
        return [_remap_nested_evidence(item, mapping, invalid) for item in value]
    return deepcopy(value)


def _recompute_set_evidence(workspace: dict[str, Any]) -> None:
    by_set: dict[str, list[str]] = {}
    for field in ("source_assessments", "source_findings", "coverage_outcomes"):
        for item in workspace.get(field) or []:
            if isinstance(item, dict) and item.get("set_id"):
                by_set.setdefault(str(item["set_id"]), []).extend(_nested_evidence_ids(item))
    for source_set in workspace.get("source_sets") or []:
        if not isinstance(source_set, dict):
            continue
        evidence_ids = list(by_set.get(str(source_set.get("set_id") or ""), []))
        for field in ("observations", "written_actions"):
            evidence_ids.extend(_nested_evidence_ids(source_set.get(field)))
        source_set["evidence_ids"] = _dedupe(evidence_ids)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "TRIAGE_REPAIR_COMPLETED",
    "TRIAGE_REPAIR_IN_PROGRESS",
    "TRIAGE_REPAIR_RESERVED",
    "carry_pending_triage_repair",
    "complete_triage_repair",
    "pending_triage_repair",
    "prepare_pending_triage_repair",
    "reconcile_carried_triage_evidence",
    "reserve_triage_repair",
    "start_triage_repair",
    "triage_repair_operation",
    "triage_workspace_evidence_ids",
]
