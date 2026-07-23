from __future__ import annotations

from hashlib import sha256
from typing import Any

from app.generation_v2.model_facing_pagination import fit_model_page
from app.generation_v2.model_facing_record_parts import composite_record_parts
from app.generation_v2.source_triage_model_views import (
    model_safe_coverage_outcome,
    model_safe_coverage_question,
    model_safe_source_assessment,
    model_safe_source_finding,
)
from app.generation_v2.source_triage_set_state_views import (
    _finding_supports_question,
    _page_metadata,
    _set_records,
    _tag_parts,
)


_REVIEW_EVIDENCE_PART_CHARS = 6_000
_REVIEW_REF_BATCH_SIZE = 30


def semantic_review_evidence_ids(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
) -> list[str]:
    set_id = str(source_set.get("set_id") or "")
    return list(
        dict.fromkeys(
            str(evidence_id)
            for finding in _set_records(workspace, "source_findings", set_id)
            for evidence_id in finding.get("evidence_ids") or []
            if str(evidence_id)
        )
    )


def semantic_review_page(
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    *,
    evidence_by_id: dict[str, dict[str, Any]],
    cursor: int,
    limit: int,
) -> dict[str, Any]:
    from app.generation_v2.source_triage_set_review import (
        semantic_review_question_ids,
        semantic_review_question_relations,
        set_semantic_review_signature,
    )

    set_id = str(source_set.get("set_id") or "")
    findings = _set_records(workspace, "source_findings", set_id)
    expected_evidence_ids = {
        str(evidence_id)
        for finding in findings
        for evidence_id in finding.get("evidence_ids") or []
        if str(evidence_id)
    }
    missing_evidence_ids = sorted(expected_evidence_ids - set(evidence_by_id))
    if missing_evidence_ids:
        return {
            "accepted": False,
            "error": "semantic_review_evidence_unavailable",
            "set_id": set_id,
            "evidence_ids": missing_evidence_ids,
            "next_action": "Retry semantic_review after the persisted evidence is readable.",
        }
    assessments = _set_records(workspace, "source_assessments", set_id)
    outcomes = _set_records(workspace, "coverage_outcomes", set_id)
    question_ids = semantic_review_question_ids(workspace, source_set)
    questions = [
        item
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") in question_ids
    ]
    parts: list[dict[str, Any]] = []
    for assessment in assessments:
        parts.extend(
            _tag_parts(
                composite_record_parts(
                    _review_source_assessment_view(assessment),
                    record_kind="source_assessment",
                ),
                "review_source_context",
            )
        )
    finding_ids_by_evidence: dict[str, list[str]] = {}
    for finding in findings:
        finding_view = model_safe_source_finding(finding)
        finding_view["evidence_previews"] = []
        parts.extend(
            _tag_parts(
                composite_record_parts(finding_view, record_kind="source_finding"),
                "review_finding",
            )
        )
        for evidence_id in finding.get("evidence_ids") or []:
            finding_ids_by_evidence.setdefault(str(evidence_id), []).append(
                str(finding.get("finding_id") or "")
            )
    for evidence_id in sorted(expected_evidence_ids):
        parts.extend(
            _tag_parts(
                _review_evidence_parts(
                    finding_ids=list(dict.fromkeys(finding_ids_by_evidence.get(evidence_id) or [])),
                    evidence=evidence_by_id[evidence_id],
                ),
                "review_evidence",
            )
        )
    outcomes_by_question = {str(item.get("question_id") or ""): item for item in outcomes}
    for parent_question_id, child_question_id in sorted(
        semantic_review_question_relations(workspace, source_set)
    ):
        parent_outcome = outcomes_by_question.get(parent_question_id) or {}
        child_outcome = outcomes_by_question.get(child_question_id) or {}
        parts.append(
            {
                "state_record_kind": "review_question_relation",
                "owner_ref": f"question_relation:{parent_question_id}:{child_question_id}",
                "record_kind": "question_relation",
                "part_kind": "core",
                "parent_question_id": parent_question_id,
                "child_question_id": child_question_id,
                "parent_outcome_ref": (
                    f"outcome:{parent_question_id}:{set_id}" if parent_outcome else ""
                ),
                "child_outcome_ref": (
                    f"outcome:{child_question_id}:{set_id}" if child_outcome else ""
                ),
                "parent_local_outcome_status": parent_outcome.get("status"),
                "child_local_outcome_status": child_outcome.get("status"),
                "both_local_outcomes_present": bool(parent_outcome and child_outcome),
            }
        )
    for question in questions:
        question_id = str(question.get("question_id") or "")
        parts.extend(
            _review_question_parts(
                question=question,
                findings=[item for item in findings if _finding_supports_question(item, question_id)],
                outcome=outcomes_by_question.get(question_id),
            )
        )
    signature = set_semantic_review_signature(workspace, source_set)
    page_parts = parts[cursor : cursor + limit]
    return fit_model_page(
        {
            "accepted": True,
            "section": "semantic_review",
            "set_id": set_id,
            "output_language": str((workspace.get("intent") or {}).get("language") or "").strip(),
            "review_snapshot_id": signature,
            "review_instruction": (
                "Judge meaning yourself. Compare every result with its exact completion criterion. Repair "
                "unsupported claims, wrong links, incomplete causality, inconsistent parent/child outcomes, weak "
                "partial-reading justifications, or model-authored prose outside output_language; then reread the "
                "new snapshot. Quotations stay in the source language. The backend checks only that the current "
                "snapshot was read."
            ),
            "items": page_parts,
            "page": {
                **_page_metadata(total=len(parts), cursor=cursor, returned=len(page_parts)),
                "unit": "review_part",
            },
        },
        requested_limit=limit,
        error_code="triage_workspace_item_exceeds_model_read_contract",
    )


def _review_source_assessment_view(assessment: dict[str, Any]) -> dict[str, Any]:
    view = model_safe_source_assessment(assessment)
    reading_basis = dict(view.get("reading_basis") or {})
    ledger = dict(reading_basis.get("coverage_ledger") or {})
    structure = dict(ledger.get("structure") or {})
    structure_items = list(structure.pop("items", []) or [])
    if structure or structure_items:
        structure["item_count"] = len(structure_items)
        structure["items_in_review"] = False
        structure["full_items_location"] = {
            "section": "source_memory",
            "source_ref": str(view.get("source_ref") or ""),
        }
        ledger["structure"] = structure
    reading_basis["coverage_ledger"] = ledger
    view["reading_basis"] = reading_basis
    return view


def _review_evidence_parts(
    *,
    finding_ids: list[str],
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence_id = str(evidence.get("evidence_id") or "")
    owner_ref = f"evidence:{evidence_id}"
    snippet = str(evidence.get("snippet_text") or "")
    metadata = {
        key: value
        for key, value in evidence.items()
        if key not in {"snippet_text", "snippet_chars", "snippet_truncated", "omitted_chars"}
    }
    chunks = [
        snippet[offset : offset + _REVIEW_EVIDENCE_PART_CHARS]
        for offset in range(0, len(snippet), _REVIEW_EVIDENCE_PART_CHARS)
    ]
    parts = [
        {
            "owner_ref": owner_ref,
            "record_kind": "review_evidence",
            "part_kind": "core",
            "finding_ids": finding_ids,
            **metadata,
            "snippet_chars": len(snippet),
            "snippet_sha256": sha256(snippet.encode("utf-8")).hexdigest(),
            "snippet_part_count": len(chunks),
        }
    ]
    parts.extend(
        {
            "owner_ref": owner_ref,
            "record_kind": "review_evidence",
            "part_kind": "snippet_part",
            "finding_ids": finding_ids,
            "evidence_id": evidence_id,
            "part_index": index,
            "start_offset": offset,
            "end_offset": offset + len(chunk),
            "text": chunk,
        }
        for index, chunk in enumerate(chunks)
        for offset in [index * _REVIEW_EVIDENCE_PART_CHARS]
    )
    return parts


def _review_question_parts(
    *,
    question: dict[str, Any],
    findings: list[dict[str, Any]],
    outcome: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    question_view = model_safe_coverage_question(question)
    outcome_view = (
        model_safe_coverage_outcome(outcome, source_ref_by_id={})
        if isinstance(outcome, dict)
        else None
    )
    links_by_requirement: dict[str, list[str]] = {}
    linked_finding_ids: list[str] = []
    for finding in findings:
        finding_id = str(finding.get("finding_id") or "")
        if finding_id:
            linked_finding_ids.append(finding_id)
        for link in finding.get("question_links") or []:
            if not isinstance(link, dict) or str(link.get("question_id") or "") != str(
                question.get("question_id") or ""
            ):
                continue
            for requirement_id in link.get("requirement_ids") or []:
                links_by_requirement.setdefault(str(requirement_id), []).append(finding_id)
    result_by_requirement = {
        str(item.get("requirement_id") or ""): item
        for item in (outcome_view or {}).get("requirement_results") or []
        if isinstance(item, dict)
    }
    requirements = []
    for requirement in question_view.get("answer_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("requirement_id") or "")
        requirements.append(
            {
                **requirement,
                "current_result": result_by_requirement.get(requirement_id),
                "allowed_finding_ids": list(
                    dict.fromkeys(links_by_requirement.get(requirement_id) or [])
                ),
            }
        )
    question_id = str(question.get("question_id") or "")
    owner_ref = f"review_question:{question_id}"
    question_core = dict(question_view)
    question_core.pop("answer_requirements", None)
    outcome_core = dict(outcome_view or {})
    outcome_core.pop("requirement_results", None)
    outcome_finding_ids = list(outcome_core.pop("finding_ids", []) or [])
    outcome_evidence_ids = list(outcome_core.pop("evidence_ids", []) or [])
    outcome_source_refs = list(outcome_core.pop("source_refs", []) or [])
    parts: list[dict[str, Any]] = [
        {
            "state_record_kind": "review_question",
            "owner_ref": owner_ref,
            "record_kind": "review_question",
            "part_kind": "core",
            "question_id": question_id,
            "parent_question_id": str(question.get("parent_question_id") or ""),
            "question": question_core,
            "current_outcome": outcome_core or None,
            "requirement_count": len(requirements),
        }
    ]
    for requirement in requirements:
        allowed_ids = list(requirement.pop("allowed_finding_ids", []) or [])
        current_result = dict(requirement.get("current_result") or {})
        result_finding_ids = list(current_result.pop("finding_ids", []) or [])
        requirement["current_result"] = current_result or None
        parts.append(
            {
                "state_record_kind": "review_question",
                "owner_ref": owner_ref,
                "record_kind": "review_question",
                "part_kind": "requirement",
                "question_id": question_id,
                **requirement,
                "allowed_finding_count": len(allowed_ids),
            }
        )
        parts.extend(
            _review_ref_parts(
                owner_ref=owner_ref,
                question_id=question_id,
                part_kind="requirement_allowed_finding_refs",
                finding_ids=allowed_ids,
                requirement_id=str(requirement.get("requirement_id") or ""),
            )
        )
        parts.extend(
            _review_ref_parts(
                owner_ref=owner_ref,
                question_id=question_id,
                part_kind="requirement_result_finding_refs",
                finding_ids=result_finding_ids,
                requirement_id=str(requirement.get("requirement_id") or ""),
            )
        )
    parts.extend(
        _review_ref_parts(
            owner_ref=owner_ref,
            question_id=question_id,
            part_kind="question_allowed_finding_refs",
            finding_ids=list(dict.fromkeys(linked_finding_ids)),
        )
    )
    parts.extend(
        _review_id_parts(
            owner_ref=owner_ref,
            question_id=question_id,
            part_kind="outcome_evidence_refs",
            values=outcome_evidence_ids,
            field="evidence_ids",
        )
    )
    parts.extend(
        _review_id_parts(
            owner_ref=owner_ref,
            question_id=question_id,
            part_kind="outcome_source_refs",
            values=outcome_source_refs,
            field="source_refs",
        )
    )
    parts.extend(
        _review_ref_parts(
            owner_ref=owner_ref,
            question_id=question_id,
            part_kind="outcome_finding_refs",
            finding_ids=outcome_finding_ids,
        )
    )
    return parts


def _review_ref_parts(
    *,
    owner_ref: str,
    question_id: str,
    part_kind: str,
    finding_ids: list[str],
    requirement_id: str = "",
) -> list[dict[str, Any]]:
    unique_ids = list(dict.fromkeys(str(value) for value in finding_ids if str(value)))
    return [
        {
            "state_record_kind": "review_question",
            "owner_ref": owner_ref,
            "record_kind": "review_question",
            "part_kind": part_kind,
            "question_id": question_id,
            **({"requirement_id": requirement_id} if requirement_id else {}),
            "batch_index": index // _REVIEW_REF_BATCH_SIZE,
            "finding_ids": unique_ids[index : index + _REVIEW_REF_BATCH_SIZE],
        }
        for index in range(0, len(unique_ids), _REVIEW_REF_BATCH_SIZE)
    ]


def _review_id_parts(
    *,
    owner_ref: str,
    question_id: str,
    part_kind: str,
    values: list[str],
    field: str,
) -> list[dict[str, Any]]:
    unique_values = list(dict.fromkeys(str(value) for value in values if str(value)))
    return [
        {
            "state_record_kind": "review_question",
            "owner_ref": owner_ref,
            "record_kind": "review_question",
            "part_kind": part_kind,
            "question_id": question_id,
            "batch_index": index // _REVIEW_REF_BATCH_SIZE,
            field: unique_values[index : index + _REVIEW_REF_BATCH_SIZE],
        }
        for index in range(0, len(unique_values), _REVIEW_REF_BATCH_SIZE)
    ]


__all__ = ["semantic_review_evidence_ids", "semantic_review_page"]
