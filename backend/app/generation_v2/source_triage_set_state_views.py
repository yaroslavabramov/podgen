from __future__ import annotations

import json
from typing import Any

from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.model_facing_record_parts import composite_record_parts
from app.generation_v2.source_triage_model_views import (
    model_safe_coverage_hint,
    model_safe_coverage_outcome,
    model_safe_coverage_question,
    model_safe_reading_note,
    model_safe_source_assessment,
    model_safe_source_finding,
    model_safe_source_read_receipt,
    reading_note_record_parts,
)
from app.generation_v2.source_triage_model_delivery import source_structure_coverage
from app.generation_v2.source_triage_question_state import (
    dependent_question_results,
    finding_catalog_snapshot_id,
    question_input_snapshot_id,
)
from app.generation_v2.source_triage_question_references import known_question_id_error
from app.generation_v2.source_triage_read_checkpointing import (
    pending_comparison_source_refs,
    pending_exact_read_source_refs,
    source_refs_with_reads_newer_than_analysis,
)
from app.generation_v2.source_triage_workspace_state import (
    latest_source_read_receipts,
)


def set_overview_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
    section: str = "set_overview",
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    assessments = _set_records(workspace, "source_assessments", set_id)
    findings = _set_records(workspace, "source_findings", set_id)
    hints = _set_records(workspace, "coverage_hints", set_id)
    receipts = [
        item
        for item in workspace.get("source_read_receipts") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    reading_notes = _set_records(workspace, "reading_notes", set_id)
    assessments_by_ref = {str(item.get("source_ref") or ""): item for item in assessments}
    findings_by_ref: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        findings_by_ref.setdefault(str(finding.get("source_ref") or ""), []).append(finding)
    questions_by_id = {
        str(item.get("question_id") or ""): item
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict)
    }
    assigned_question_ids = [
        str(value)
        for value in source_set.get("assigned_question_ids", source_set.get("question_ids") or [])
        if str(value)
    ]
    cross_question_ids = [str(value) for value in source_set.get("cross_question_ids") or [] if str(value)]
    discovered_question_ids = sorted(
        ({
            str(link.get("question_id") or "")
            for finding in findings
            for link in finding.get("question_links") or []
            if isinstance(link, dict) and str(link.get("question_id") or "")
        }
        | {
            str(hint.get("question_id") or "")
            for hint in hints
            if isinstance(hint, dict) and str(hint.get("question_id") or "")
        })
        - set(assigned_question_ids)
    )
    question_ids = [*assigned_question_ids, *discovered_question_ids]
    resolved_refs = {
        source_ref
        for source_ref, item in assessments_by_ref.items()
        if str(item.get("processing_status") or "resolved") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
    }
    items: list[dict[str, Any]] = []
    for source_ref in source_set.get("source_refs") or []:
        ref = str(source_ref)
        assessment = assessments_by_ref.get(ref)
        items.append(
            {
                "item_kind": "source",
                "source_ref": ref,
                "status": "resolved" if ref in resolved_refs else "pending",
                "representation_status": (assessment or {}).get("representation_status"),
                "disposition": (assessment or {}).get("disposition"),
                "finding_count": len(findings_by_ref.get(ref, [])),
                "coverage_hint_count": sum(1 for hint in hints if str(hint.get("source_ref") or "") == ref),
                "next_action": "source_memory" if assessment is not None else "read_source",
            }
        )
    for question_id in question_ids:
        question = questions_by_id.get(question_id) or {}
        items.append(
            {
                "item_kind": "question",
                "question_id": question_id,
                "parent_question_id": question.get("parent_question_id"),
                "role": "assigned" if question_id in set(assigned_question_ids) else "discovered_hint",
                "linked_finding_count": sum(
                    1
                    for finding in findings
                    if _finding_supports_question(finding, question_id)
                ),
                "coverage_hint_count": sum(
                    1 for hint in hints if str(hint.get("question_id") or "") == question_id
                ),
                "next_action": "question_index",
            }
        )
    pending_exact_refs = pending_exact_read_source_refs(workspace, source_set=source_set)
    pending_comparison_refs = pending_comparison_source_refs(source_set)
    stale_analysis_refs = source_refs_with_reads_newer_than_analysis(workspace, set_id=set_id)
    page_items = items[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": section,
            "set_id": set_id,
            "output_language": _output_language(workspace),
            "state": {
                "pending_source_refs": [
                    str(value) for value in source_set.get("source_refs") or [] if str(value) not in resolved_refs
                ],
                "pending_exact_read_source_refs": pending_exact_refs,
                "pending_comparison_source_refs": pending_comparison_refs,
                "stale_source_analysis_refs": stale_analysis_refs,
                "assigned_question_ids": assigned_question_ids,
                "cross_question_ids": cross_question_ids,
                "discovered_question_ids": discovered_question_ids,
                "saved_source_analysis_count": len(assessments),
                "source_read_receipt_count": len(receipts),
                "latest_source_read_receipts": [
                    model_safe_source_read_receipt(item)
                    for item in latest_source_read_receipts(receipts)
                ],
                "saved_reading_note_count": len(reading_notes),
                "saved_finding_count": len(findings),
                "saved_coverage_hint_count": len(hints),
            },
            "items": page_items,
            "page": _page_metadata(total=len(items), cursor=cursor, returned=len(page_items)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def source_memory_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    source_ref: str,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    if source_ref not in {str(value) for value in source_set.get("source_refs") or []}:
        return {
            "accepted": False,
            "error": "source_memory_requires_current_set_source_ref",
            "source_ref": source_ref,
            "allowed_source_refs": [str(value) for value in source_set.get("source_refs") or []],
            "next_action": "Copy one source_ref from set_overview and retry.",
        }
    records: list[dict[str, Any]] = []
    structure = source_structure_coverage(
        workspace,
        set_id=set_id,
        source_ref=source_ref,
    )
    if structure:
        records.extend(
            _tag_parts(
                composite_record_parts(
                    structure,
                    record_kind="source_structure_page",
                    owner_ref=f"source_structure:{set_id}:{source_ref}",
                ),
                "source_structure_page",
            )
        )
    for item in _set_records(workspace, "source_read_receipts", set_id):
        if str(item.get("source_ref") or "") == source_ref:
            records.extend(
                _tag_parts(
                    composite_record_parts(
                        model_safe_source_read_receipt(item),
                        record_kind="source_read_receipt",
                        owner_ref=str(item.get("receipt_ref") or "source_read_receipt:unknown"),
                    ),
                    "source_read_receipt",
                )
            )
    for item in _set_records(workspace, "reading_notes", set_id):
        if str(item.get("source_ref") or "") == source_ref:
            records.extend(_tag_parts(reading_note_record_parts(model_safe_reading_note(item)), "reading_note"))
    for field, kind, converter in (
        ("source_assessments", "source_assessment", model_safe_source_assessment),
        ("source_findings", "source_finding", model_safe_source_finding),
    ):
        for item in _set_records(workspace, field, set_id):
            if str(item.get("source_ref") or "") != source_ref:
                continue
            records.extend(
                _tag_parts(
                    composite_record_parts(converter(item), record_kind=kind),
                    kind,
                )
            )
    return _parts_page(
        section="source_memory",
        set_id=set_id,
        records=records,
        cursor=cursor,
        limit=limit,
        metadata={"source_ref": source_ref, "output_language": _output_language(workspace)},
    )


def question_memory_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    question_id: str,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    question = next(
        (
            item
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
        ),
        None,
    )
    findings = [
        item
        for item in _set_records(workspace, "source_findings", set_id)
        if _finding_supports_question(item, question_id)
    ]
    if question is None:
        return known_question_id_error(
            workspace,
            question_id,
            error="question_memory_requires_known_question_id",
        )
    records = _question_packet_parts(
        question=question,
        findings=findings,
        outcomes=[
            item
            for item in _set_records(workspace, "coverage_outcomes", set_id)
            if str(item.get("question_id") or "") == question_id
        ],
    )
    return _parts_page(
        section="question_memory",
        set_id=set_id,
        records=records,
        cursor=cursor,
        limit=limit,
        metadata={"question_id": question_id, "output_language": _output_language(workspace)},
    )


def question_workbench_signature(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    question_id: str,
) -> str:
    """Backward-compatible name for the question input snapshot."""

    return question_input_snapshot_id(workspace, source_set, question_id=question_id)


def finding_catalog_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    findings = _set_records(workspace, "source_findings", set_id)
    records: list[dict[str, Any]] = []
    for finding in findings:
        records.extend(
            _tag_parts(
                composite_record_parts(
                    _finding_catalog_item(finding),
                    record_kind="candidate_source_finding",
                    owner_ref=str(finding.get("finding_id") or "source_finding:unknown"),
                ),
                "finding_catalog_item",
            )
        )
    return _parts_page(
        section="finding_catalog",
        set_id=set_id,
        records=records,
        cursor=cursor,
        limit=limit,
        metadata={
            "finding_catalog_snapshot_id": finding_catalog_snapshot_id(
                workspace,
                source_set,
            ),
            "finding_count": len(findings),
            "output_language": _output_language(workspace),
            "evidence_access": (
                "Use read_evidence with a listed evidence_id only when exact support or directness is doubtful."
            ),
        },
    )


def question_workbench_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    question_id: str,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    question = next(
        (
            item
            for item in workspace.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
        ),
        None,
    )
    if question is None:
        return known_question_id_error(
            workspace,
            question_id,
            error="question_workbench_requires_known_question_id",
        )
    findings = _set_records(workspace, "source_findings", set_id)
    catalog_snapshot_id = finding_catalog_snapshot_id(workspace, source_set)
    suggested_finding_ids = [
        str(item.get("finding_id") or "")
        for item in findings
        if question_id in {str(value) for value in item.get("possible_question_ids") or []}
    ]
    all_finding_ids = [str(item.get("finding_id") or "") for item in findings]
    records = _tag_parts(
        composite_record_parts(
            model_safe_coverage_question(question),
            record_kind="coverage_question",
            owner_ref=f"question:{question_id}",
        ),
        "question",
    )
    records.extend(
        _tag_parts(
            composite_record_parts(
                {
                    "question_id": question_id,
                    "finding_catalog_snapshot_id": catalog_snapshot_id,
                    "suggested_finding_ids": suggested_finding_ids,
                    "all_finding_ids": all_finding_ids,
                    "candidate_rule": (
                        "suggested_finding_ids come from source analysis and are not exhaustive; any finding in "
                        "the current catalog may be selected after semantic comparison."
                    ),
                    "coverage_rule": (
                        "Covered must satisfy both the requirement completion_criterion and the full question text. "
                        "If a requirement is broader than the question, the question's actor, period, mechanism, "
                        "comparison, and scope are the tighter contract."
                    ),
                },
                record_kind="question_finding_index",
                owner_ref=f"question:{question_id}:finding_index",
            ),
            "question_finding_index",
        )
    )
    for finding in findings:
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id:
            continue
        records.extend(
            _tag_parts(
                composite_record_parts(
                    _question_workbench_candidate_finding(
                        finding,
                        question_id=question_id,
                    ),
                    record_kind="candidate_source_finding",
                    owner_ref=f"question:{question_id}:finding:{finding_id}",
                ),
                "question_candidate_finding",
            )
        )
    dependency_records = dependent_question_results(
        workspace,
        source_set,
        question_id=question_id,
    )
    for dependency in dependency_records:
        child = dict(dependency.get("question") or {})
        child_id = str(child.get("question_id") or "")
        child_outcome = dependency.get("outcome")
        dependency_ref = f"question:{question_id}:dependency:{child_id}"
        records.extend(
            _tag_parts(
                composite_record_parts(
                    {
                        "child_question_id": child_id,
                        "outcome_present": isinstance(child_outcome, dict),
                        "outcome_is_current": bool(dependency.get("outcome_is_current")),
                        "outcome_ref": (
                            f"outcome:{child_id}:{set_id}" if isinstance(child_outcome, dict) else ""
                        ),
                    },
                    record_kind="dependent_question_result",
                    owner_ref=dependency_ref,
                ),
                "dependent_question_result",
            )
        )
        records.extend(
            _tag_parts(
                composite_record_parts(
                    model_safe_coverage_question(child),
                    record_kind="coverage_question",
                    owner_ref=f"{dependency_ref}:question",
                ),
                "dependent_question",
            )
        )
        if isinstance(child_outcome, dict):
            records.extend(
                _tag_parts(
                    composite_record_parts(
                        model_safe_coverage_outcome(child_outcome, source_ref_by_id={}),
                        record_kind="coverage_outcome",
                        owner_ref=f"{dependency_ref}:outcome",
                    ),
                    "dependent_question_outcome",
                )
            )
    for outcome in _set_records(workspace, "coverage_outcomes", set_id):
        if str(outcome.get("question_id") or "") != question_id:
            continue
        records.extend(
            _tag_parts(
                composite_record_parts(
                    model_safe_coverage_outcome(outcome, source_ref_by_id={}),
                    record_kind="coverage_outcome",
                ),
                "current_outcome",
            )
        )
    return _parts_page(
        section="question_workbench",
        set_id=set_id,
        records=records,
        cursor=cursor,
        limit=limit,
        metadata={
            "question_id": question_id,
            "finding_catalog_snapshot_id": catalog_snapshot_id,
            "finding_catalog_must_be_fully_read": True,
            "dependent_question_ids": [
                str((item.get("question") or {}).get("question_id") or "")
                for item in dependency_records
            ],
            "unresolved_dependent_question_ids": [
                str((item.get("question") or {}).get("question_id") or "")
                for item in dependency_records
                if not bool(item.get("outcome_is_current"))
            ],
            "workbench_snapshot_id": question_workbench_signature(
                workspace,
                source_set,
                question_id=question_id,
            ),
            "output_language": _output_language(workspace),
        },
    )


def question_index_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    assigned_question_ids = {
        str(value)
        for value in source_set.get("assigned_question_ids", source_set.get("question_ids") or [])
        if str(value)
    }
    hinted_question_ids = {
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_hints") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("question_id") or "")
    }
    finding_question_ids = {
        str(link.get("question_id") or "")
        for finding in _set_records(workspace, "source_findings", set_id)
        for link in finding.get("question_links") or []
        if isinstance(link, dict) and str(link.get("question_id") or "")
    }
    records: list[dict[str, Any]] = []
    for question in workspace.get("coverage_questions") or []:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("question_id") or "")
        if not question_id:
            continue
        payload = model_safe_coverage_question(question)
        payload["set_role"] = (
            "assigned"
            if question_id in assigned_question_ids
            else "linked_or_hinted"
            if question_id in hinted_question_ids or question_id in finding_question_ids
            else "available"
        )
        records.extend(
            _tag_parts(
                composite_record_parts(
                    payload,
                    record_kind="coverage_question",
                    owner_ref=f"question:{question_id}",
                ),
                "question_index_item",
            )
        )
    return _parts_page(
        section="question_index",
        set_id=set_id,
        records=records,
        cursor=cursor,
        limit=limit,
        metadata={
            "output_language": _output_language(workspace),
            "question_count": len(
                [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
            ),
        },
    )


def coverage_hints_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    source_ref: str = "",
    question_id: str = "",
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    hints = [
        item
        for item in _set_records(workspace, "coverage_hints", set_id)
        if (not source_ref or str(item.get("source_ref") or "") == source_ref)
        and (not question_id or str(item.get("question_id") or "") == question_id)
    ]
    records = [
        part
        for hint in hints
        for part in _tag_parts(
            composite_record_parts(
                model_safe_coverage_hint(hint),
                record_kind="coverage_hint",
                owner_ref=str(hint.get("hint_id") or "coverage_hint:unknown"),
            ),
            "coverage_hint",
        )
    ]
    return _parts_page(
        section="coverage_hints",
        set_id=set_id,
        records=records,
        cursor=cursor,
        limit=limit,
        metadata={
            "source_ref": source_ref,
            "question_id": question_id,
            "output_language": _output_language(workspace),
            "hint_count": len(hints),
        },
    )


def unfinished_work_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    overview = set_overview_page(workspace, source_set, cursor=0, limit=25)
    state = dict(overview.get("state") or {})
    items: list[dict[str, Any]] = []
    for field, kind in (
        ("pending_source_refs", "source_analysis_missing"),
        ("pending_exact_read_source_refs", "exact_read_not_reconciled"),
        ("pending_comparison_source_refs", "comparison_not_reconciled"),
        ("stale_source_analysis_refs", "source_analysis_stale"),
    ):
        items.extend({"work_kind": kind, "source_ref": str(value)} for value in state.get(field) or [])
    page_items = items[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "unfinished_work",
            "set_id": str(source_set.get("set_id") or ""),
            "output_language": _output_language(workspace),
            "items": page_items,
            "page": _page_metadata(total=len(items), cursor=cursor, returned=len(page_items)),
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _output_language(workspace: dict[str, Any]) -> str:
    return str((workspace.get("intent") or {}).get("language") or "").strip()


def _question_packet_parts(
    *,
    question: dict[str, Any],
    findings: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    review: bool = False,
) -> list[dict[str, Any]]:
    question_id = str(question.get("question_id") or "")
    prefix = "review_" if review else ""
    parts = _tag_parts(
        composite_record_parts(
            model_safe_coverage_question(question),
            record_kind="coverage_question",
            owner_ref=f"question:{question_id}",
        ),
        f"{prefix}question",
    )
    for requirement in question.get("answer_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("requirement_id") or "")
        owner_ref = f"question:{question_id}:requirement:{requirement_id}"
        matching = [
            finding
            for finding in findings
            if _finding_supports_requirement(finding, question_id, requirement_id)
        ]
        parts.extend(
            {
                "state_record_kind": f"{prefix}requirement_findings",
                "owner_ref": owner_ref,
                "record_kind": "requirement_findings",
                "part_kind": "allowed_finding",
                "index": index,
                "finding_id": str(finding.get("finding_id") or ""),
                "source_ref": str(finding.get("source_ref") or ""),
                "statement": str(finding.get("statement") or ""),
            }
            for index, finding in enumerate(matching)
        )
    for finding in findings:
        if not _finding_supports_question(finding, question_id):
            continue
        parts.extend(
            _tag_parts(
                composite_record_parts(
                    model_safe_source_finding(finding),
                    record_kind="source_finding",
                    owner_ref=str(finding.get("finding_id") or "source_finding:unknown"),
                ),
                f"{prefix}finding",
            )
        )
    for outcome in outcomes:
        parts.extend(
            _tag_parts(
                composite_record_parts(
                    model_safe_coverage_outcome(outcome, source_ref_by_id={}),
                    record_kind="coverage_outcome",
                ),
                f"{prefix}outcome",
            )
        )
    return parts


def _parts_page(
    *,
    section: str,
    set_id: str,
    records: list[dict[str, Any]],
    cursor: int,
    limit: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    page_records = records[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": section,
            "set_id": set_id,
            **metadata,
            "items": page_records,
            "page": {
                **_page_metadata(total=len(records), cursor=cursor, returned=len(page_records)),
                "unit": "record_part",
            },
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _tag_parts(parts: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [{"state_record_kind": kind, **part} for part in parts]


def _set_records(workspace: dict[str, Any], field: str, set_id: str) -> list[dict[str, Any]]:
    return sorted(
        [
            item
            for item in workspace.get(field) or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ],
        key=lambda item: json.dumps(
            {
                key: item.get(key)
                for key in ("source_ref", "question_id", "finding_id", "hint_id", "note_id", "receipt_ref")
            },
            sort_keys=True,
            default=str,
        ),
    )


def _finding_supports_question(finding: dict[str, Any], question_id: str) -> bool:
    return any(
        isinstance(link, dict) and str(link.get("question_id") or "") == question_id
        for link in finding.get("question_links") or []
    )


def _question_workbench_finding(finding: dict[str, Any]) -> dict[str, Any]:
    payload = model_safe_source_finding(finding)
    # Links for other questions and finalizer-owned source relations do not
    # affect this question's evidence decision and must not invalidate it.
    payload["question_links"] = []
    payload["source_relations"] = []
    return payload


def _finding_catalog_item(finding: dict[str, Any]) -> dict[str, Any]:
    payload = _question_workbench_finding(finding)
    for field in (
        "set_id",
        "passage_refs",
        "evidence_previews",
        "semantic_revision",
        "updated_at",
    ):
        payload.pop(field, None)
    return payload


def _question_workbench_candidate_finding(
    finding: dict[str, Any],
    *,
    question_id: str,
) -> dict[str, Any]:
    payload = _question_workbench_finding(finding)
    possible_question_ids = [str(value) for value in finding.get("possible_question_ids") or []]
    payload["suggested_for_question"] = question_id in set(possible_question_ids)
    payload["current_question_links"] = [
        {
            "requirement_ids": [str(value) for value in link.get("requirement_ids") or []],
            "rationale": str(link.get("rationale") or ""),
        }
        for link in finding.get("question_links") or []
        if isinstance(link, dict) and str(link.get("question_id") or "") == question_id
    ]
    for field in (
        "set_id",
        "passage_refs",
        "evidence_previews",
        "source_relations",
        "question_links",
        "semantic_revision",
        "updated_at",
    ):
        payload.pop(field, None)
    return payload


def _finding_supports_requirement(finding: dict[str, Any], question_id: str, requirement_id: str) -> bool:
    return any(
        isinstance(link, dict)
        and str(link.get("question_id") or "") == question_id
        and requirement_id in {str(value) for value in link.get("requirement_ids") or []}
        for link in finding.get("question_links") or []
    )


def _page_metadata(*, total: int, cursor: int, returned: int) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
    }


__all__ = [
    "coverage_hints_page",
    "finding_catalog_page",
    "question_index_page",
    "question_memory_page",
    "question_workbench_page",
    "question_workbench_signature",
    "set_overview_page",
    "source_memory_page",
    "unfinished_work_page",
]
