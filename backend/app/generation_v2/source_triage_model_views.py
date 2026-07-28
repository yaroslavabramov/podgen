from __future__ import annotations

from typing import Any

from app.generation_v2.source_triage_workspace_state import (
    source_read_receipt_delivery_status,
)
from app.generation_v2.tools.evidence_source_passage_handles import (
    model_passage_refs_for_receipt,
)


def model_safe_coverage_question(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": item.get("question_id"),
        "question": str(item.get("question") or ""),
        "kind": item.get("kind"),
        "importance": item.get("importance"),
        "rationale": str(item.get("rationale") or ""),
        "parent_question_id": item.get("parent_question_id"),
        "origin": item.get("origin"),
        "answer_requirements": [
            {
                **{
                    "requirement_id": requirement.get("requirement_id"),
                    "description": str(requirement.get("description") or ""),
                    "rationale": str(requirement.get("rationale") or ""),
                },
                **(
                    {"completion_criterion": str(requirement.get("completion_criterion") or "")}
                    if "completion_criterion" in requirement
                    else {}
                ),
            }
            for requirement in item.get("answer_requirements") or []
            if isinstance(requirement, dict)
        ],
    }


def model_safe_source_read_receipt(item: dict[str, Any]) -> dict[str, Any]:
    passage_refs = model_passage_refs_for_receipt(item)
    return {
        "receipt_ref": str(item.get("receipt_ref") or ""),
        "set_id": str(item.get("set_id") or ""),
        "source_ref": str(item.get("source_ref") or ""),
        "start_offset": item.get("start_offset"),
        "end_offset": item.get("end_offset"),
        "total_chars": item.get("total_chars"),
        "read_kind": str(item.get("read_kind") or ""),
        "passage_refs": passage_refs,
        "passage_refs_require_hydration": bool(
            item.get("passage_refs") and not passage_refs
        ),
        "replay_passages": [
            {
                "passage_ref": str(passage.get("passage_ref") or ""),
                "text": str(passage.get("text") or ""),
                "range": dict(passage.get("range") or {}),
                "locator": str(passage.get("locator") or ""),
                "locator_type": str(passage.get("locator_type") or ""),
            }
            for passage in item.get("replay_passages") or []
            if isinstance(passage, dict)
        ],
        "request": dict(item.get("request") or {}),
        "delivery_status": source_read_receipt_delivery_status(item),
        "next_cursor": str(item.get("next_cursor") or ""),
        "eof": bool(item.get("eof")),
        "created_at": item.get("created_at"),
    }


def model_safe_reading_note(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "note_id": str(item.get("note_id") or ""),
        "set_id": str(item.get("set_id") or ""),
        "source_ref": str(item.get("source_ref") or ""),
        "receipt_refs": [str(value) for value in item.get("receipt_refs") or []],
        "what_was_read": str(item.get("what_was_read") or ""),
        "what_was_established": str(item.get("what_was_established") or ""),
        "boundaries": str(item.get("boundaries") or ""),
        "unread_regions": str(item.get("unread_regions") or ""),
        "unread_risk_assessment": str(
            item.get("unread_risk_assessment")
            or item.get("why_unread_is_safe")
            or ""
        ),
        "question_links": [
            {
                "question_id": str(link.get("question_id") or ""),
                "connection": str(link.get("connection") or ""),
            }
            for link in item.get("question_links") or []
            if isinstance(link, dict)
        ],
        "doubts": [str(value) for value in item.get("doubts") or []],
        "next_checks": [str(value) for value in item.get("next_checks") or []],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def reading_note_record_parts(note: dict[str, Any]) -> list[dict[str, Any]]:
    """Page a note by whole semantic fields; no semantic scalar is ever sliced."""

    owner_ref = str(note.get("note_id") or "reading_note:unknown")
    receipt_refs = [str(value) for value in note.get("receipt_refs") or []]
    question_links = [item for item in note.get("question_links") or [] if isinstance(item, dict)]
    doubts = [str(value) for value in note.get("doubts") or []]
    next_checks = [str(value) for value in note.get("next_checks") or []]
    parts: list[dict[str, Any]] = [
        {
            "owner_ref": owner_ref,
            "record_kind": "reading_note",
            "part_kind": "core",
            "note_id": note.get("note_id"),
            "set_id": note.get("set_id"),
            "source_ref": note.get("source_ref"),
            "receipt_ref_count": len(receipt_refs),
            "question_link_count": len(question_links),
            "doubt_count": len(doubts),
            "next_check_count": len(next_checks),
            "created_at": note.get("created_at"),
            "updated_at": note.get("updated_at"),
        }
    ]
    for field in (
        "what_was_read",
        "what_was_established",
        "boundaries",
        "unread_regions",
        "unread_risk_assessment",
    ):
        parts.append(
            {
                "owner_ref": owner_ref,
                "record_kind": "reading_note",
                "part_kind": "semantic_field",
                "field": field,
                "value": str(note.get(field) or ""),
            }
        )
    parts.extend(
        {
            "owner_ref": owner_ref,
            "record_kind": "reading_note",
            "part_kind": "receipt_ref",
            "index": index,
            "receipt_ref": receipt_ref,
        }
        for index, receipt_ref in enumerate(receipt_refs)
    )
    parts.extend(
        {
            "owner_ref": owner_ref,
            "record_kind": "reading_note",
            "part_kind": "question_link",
            "index": index,
            "question_id": str(link.get("question_id") or ""),
            "connection": str(link.get("connection") or ""),
        }
        for index, link in enumerate(question_links)
    )
    for field, values in (("doubts", doubts), ("next_checks", next_checks)):
        parts.extend(
            {
                "owner_ref": owner_ref,
                "record_kind": "reading_note",
                "part_kind": "semantic_list_item",
                "field": field,
                "index": index,
                "value": value,
            }
            for index, value in enumerate(values)
        )
    return parts


def model_safe_source_assessment(item: dict[str, Any]) -> dict[str, Any]:
    source_ref = str(item.get("source_ref") or "")
    payload = {
        "assessment_ref": f"assessment:{source_ref}" if source_ref else "",
        "set_id": item.get("set_id"),
        "source_ref": source_ref,
        "processing_status": item.get("processing_status"),
        "representation_status": item.get("representation_status"),
        "representation_reason": str(item.get("representation_reason") or ""),
        "relevance": item.get("relevance"),
        "relevance_reason": str(item.get("relevance_reason") or ""),
        "authority": item.get("authority"),
        "authority_reason": str(item.get("authority_reason") or ""),
        "uniqueness": item.get("uniqueness"),
        "uniqueness_reason": str(item.get("uniqueness_reason") or ""),
        "duplicate_of_source_ref": str(item.get("duplicate_of_source_ref") or ""),
        "disposition": item.get("disposition"),
        "disposition_reason": str(item.get("disposition_reason") or ""),
        "coverage_question_ids": list(item.get("coverage_question_ids") or []),
        "editorial_functions": list(item.get("editorial_functions") or []),
        "evidence_ids": list(item.get("evidence_ids") or []),
        "finding_ids": list(item.get("finding_ids") or []),
        "reading_basis": model_safe_reading_basis(item.get("reading_basis")),
        "source_summary": str(item.get("source_summary") or ""),
        "no_findings_reason": str(item.get("no_findings_reason") or ""),
        "limitations": list(item.get("limitations") or []),
        "confidence": item.get("confidence"),
        "updated_at": item.get("updated_at"),
    }
    if item.get("repair_baseline"):
        payload["repair_baseline"] = True
        payload["repair_baseline_version"] = int(item.get("repair_baseline_version") or 1)
        if "original_set_id" in item:
            payload["original_set_id"] = item.get("original_set_id")
        if item.get("repair_baseline_evidence_unavailable"):
            payload["repair_baseline_evidence_unavailable"] = True
            payload["repair_baseline_unavailable_evidence_count"] = int(
                item.get("repair_baseline_unavailable_evidence_count") or 0
            )
    return payload


def model_safe_reading_basis(value: Any) -> dict[str, Any]:
    basis = value if isinstance(value, dict) else {}
    receipt_refs = [str(item) for item in basis.get("receipt_refs") or [] if str(item)]
    structure_receipt_refs = [
        str(item) for item in basis.get("structure_receipt_refs") or [] if str(item)
    ]
    return {
        "receipt_count": len(receipt_refs),
        "structure_page_count": len(structure_receipt_refs),
        "coverage_summary": str(basis.get("coverage_summary") or ""),
        "unread_regions": str(basis.get("unread_regions") or ""),
        "unread_risk_assessment": str(basis.get("unread_risk_assessment") or ""),
        "representation_limits": str(basis.get("representation_limits") or ""),
        "stopping_reason": str(basis.get("stopping_reason") or ""),
        "coverage_ledger": dict(basis.get("coverage_ledger") or {}),
    }


def model_safe_source_finding(item: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "finding_id": str(item.get("finding_id") or ""),
        "set_id": str(item.get("set_id") or ""),
        "source_ref": str(item.get("source_ref") or ""),
        "statement": str(item.get("statement") or ""),
        "support_explanation": str(item.get("support_explanation") or ""),
        "evidence_ids": [str(value) for value in item.get("evidence_ids") or []],
        "passage_refs": [str(value) for value in item.get("passage_refs") or []],
        "possible_question_ids": [
            str(value) for value in item.get("possible_question_ids") or []
        ],
        "evidence_previews": [
            {
                "evidence_id": str(preview.get("evidence_id") or ""),
                "snippet_preview": str(preview.get("snippet_preview") or ""),
                "snippet_chars": int(preview.get("snippet_chars") or 0),
                "snippet_truncated": bool(preview.get("snippet_truncated")),
                "locator": str(preview.get("locator") or ""),
            }
            for preview in item.get("evidence_previews") or []
            if isinstance(preview, dict)
        ],
        "scope": dict(item.get("scope") or {}),
        "limitations": [str(value) for value in item.get("limitations") or []],
        "question_links": [
            {
                "question_id": str(link.get("question_id") or ""),
                "requirement_ids": [str(value) for value in link.get("requirement_ids") or []],
                "rationale": str(link.get("rationale") or ""),
            }
            for link in item.get("question_links") or []
            if isinstance(link, dict)
        ],
        "source_relations": [
            {
                "related_source_ref": str(relation.get("related_source_ref") or ""),
                "relationship": relation.get("relationship"),
                "rationale": str(relation.get("rationale") or ""),
                "evidence_ids": [str(value) for value in relation.get("evidence_ids") or []],
            }
            for relation in item.get("source_relations") or []
            if isinstance(relation, dict) and str(relation.get("related_source_ref") or "").strip()
        ],
        "semantic_revision": int(item.get("semantic_revision") or 0),
        "updated_at": item.get("updated_at"),
    }
    if item.get("repair_baseline"):
        payload["repair_baseline"] = True
    return payload


def model_safe_coverage_outcome(
    item: dict[str, Any],
    *,
    source_ref_by_id: dict[str, str],
    include_next_action: bool = False,
) -> dict[str, Any]:
    source_refs = [str(value) for value in item.get("source_refs") or [] if str(value).strip()]
    if not source_refs:
        source_refs = [
            source_ref_by_id[str(source_id)]
            for source_id in item.get("source_ids") or []
            if source_ref_by_id.get(str(source_id))
        ]
    source_refs = list(dict.fromkeys(source_refs))
    set_id = str(item.get("set_id") or "")
    question_id = str(item.get("question_id") or "")
    payload = {
        "outcome_ref": f"outcome:{question_id}:{set_id}" if question_id and set_id else "",
        "set_id": set_id,
        "question_id": question_id,
        "status": item.get("status"),
        "source_refs": source_refs,
        "evidence_ids": list(item.get("evidence_ids") or []),
        "finding_ids": list(item.get("finding_ids") or []),
        "requirement_results": [
            {
                "requirement_id": str(result.get("requirement_id") or ""),
                "status": result.get("status"),
                "finding_ids": [str(value) for value in result.get("finding_ids") or []],
                "answer_points": [
                    {
                        "point": str(point.get("point") or ""),
                        "finding_ids": [str(value) for value in point.get("finding_ids") or []],
                    }
                    for point in result.get("answer_points") or []
                    if isinstance(point, dict)
                ],
                "summary": str(result.get("summary") or ""),
                "closure_basis": str(result.get("closure_basis") or ""),
                "remaining_uncertainty": str(result.get("remaining_uncertainty") or ""),
                "missing_detail": str(result.get("missing_detail") or ""),
                "conflict_summary": str(result.get("conflict_summary") or ""),
                "deferred_reason": str(result.get("deferred_reason") or ""),
                "confidence": result.get("confidence"),
                "next_action": result.get("next_action"),
            }
            for result in item.get("requirement_results") or []
            if isinstance(result, dict)
        ],
        "orientation_summary": str(item.get("orientation_summary") or ""),
        "missing_detail": str(item.get("missing_detail") or ""),
        "conflict_summary": str(item.get("conflict_summary") or ""),
        "deferred_reason": str(item.get("deferred_reason") or ""),
        "confidence": item.get("confidence"),
        "semantic_revision": int(item.get("semantic_revision") or 0),
        "is_stale": bool(item.get("is_stale")),
        "updated_at": item.get("updated_at"),
    }
    if item.get("repair_baseline"):
        payload["repair_baseline"] = True
        payload["repair_baseline_version"] = int(item.get("repair_baseline_version") or 1)
        if item.get("repair_baseline_evidence_unavailable"):
            payload["repair_baseline_evidence_unavailable"] = True
            payload["repair_baseline_unavailable_evidence_count"] = int(
                item.get("repair_baseline_unavailable_evidence_count") or 0
            )
    if include_next_action:
        payload["next_action"] = item.get("next_action")
    return payload


__all__ = [
    "model_safe_coverage_outcome",
    "model_safe_coverage_question",
    "model_safe_reading_basis",
    "model_safe_reading_note",
    "model_safe_source_assessment",
    "model_safe_source_finding",
    "model_safe_source_read_receipt",
    "reading_note_record_parts",
]
