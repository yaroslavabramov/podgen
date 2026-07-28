from __future__ import annotations

from typing import Any

from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_read_state import (
    canonical_source_read_state,
    source_read_state_signature,
)
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS
from app.generation_v2.source_triage_workspace_state import (
    source_read_receipt_is_delivered,
)


def validate_reading_basis(
    *,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    source_ref: str,
    representation_status: str,
    raw_basis: Any,
    verified_exact_duplicate_of_source_ref: str = "",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate the semantic stopping judgment and bind receipts server-side."""

    if not isinstance(raw_basis, dict):
        return {}, {
            "accepted": False,
            "error": "reading_basis must be an object",
        }
    set_id = str(source_set.get("set_id") or "")
    receipt_refs = sorted(
        {
            str(item.get("receipt_ref") or "")
            for item in workspace.get("source_read_receipts") or []
            if isinstance(item, dict)
            and str(item.get("set_id") or "") == set_id
            and str(item.get("source_ref") or "") == source_ref
            and str(item.get("receipt_ref") or "")
            and source_read_receipt_is_delivered(item)
        }
    )
    verified_duplicate = str(verified_exact_duplicate_of_source_ref or "").strip()
    if (
        representation_status in {"readable", "partial"}
        and not receipt_refs
        and not verified_duplicate
    ):
        return {}, {
            "accepted": False,
            "error": "A readable or partial source analysis requires delivered source reading",
            "next_action": "Read the actual source text, then save the completed analysis.",
        }

    semantic_fields = {
        "coverage_summary": str(raw_basis.get("coverage_summary") or "").strip(),
        "unread_regions": str(raw_basis.get("unread_regions") or "").strip(),
        "unread_risk_assessment": str(raw_basis.get("unread_risk_assessment") or "").strip(),
        "representation_limits": str(raw_basis.get("representation_limits") or "").strip(),
        "stopping_reason": str(raw_basis.get("stopping_reason") or "").strip(),
    }
    required_fields = ["coverage_summary", "unread_risk_assessment", "stopping_reason"]
    missing_fields = [field for field in required_fields if not semantic_fields[field]]
    if missing_fields:
        return {}, {
            "accepted": False,
            "error": "reading_basis is missing the model's stopping judgment",
            "missing_fields": missing_fields,
        }
    field_error = semantic_field_error(
        semantic_fields,
        {field: TRIAGE_SEMANTIC_FIELD_LIMITS["reading_basis"] for field in semantic_fields},
        list(semantic_fields),
    )
    if field_error:
        return {}, field_error
    read_state = canonical_source_read_state(
        workspace,
        set_id=set_id,
        source_ref=source_ref,
    )
    ledger = dict(read_state["coverage_ledger"])
    structure = dict(ledger.get("structure") or {})
    has_native_read = any(
        isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and str(item.get("read_kind") or "") == "native_file_attachment"
        and source_read_receipt_is_delivered(item)
        for item in workspace.get("source_read_receipts") or []
    )
    partial_local_read = (
        representation_status in {"readable", "partial"}
        and not ledger.get("eof_reached")
        and int(ledger.get("unread_char_count") or 0) > 0
        and not has_native_read
    )
    if partial_local_read and not verified_duplicate and not structure.get("complete"):
        next_structure_cursor = str(structure.get("next_unread_cursor") or "0")
        next_read_cursor = _next_sequential_read_cursor(
            workspace,
            set_id=set_id,
            source_ref=source_ref,
        )
        return {}, {
            "accepted": False,
            "error": "partial_read_requires_complete_structure",
            "source_ref": source_ref,
            "structure_progress": {
                "returned_count": int(structure.get("returned_count") or 0),
                "total_count": int(structure.get("total_count") or 0),
                "next_cursor": next_structure_cursor,
            },
            "allowed_next_actions": [
                {
                    "tool": "inspect_source",
                    "arguments": {
                        "source_ref": source_ref,
                        "cursor": next_structure_cursor,
                        "max_items": 24,
                    },
                    "repeat_until": "next_cursor is empty",
                },
                {
                    "tool": "read_source",
                    "arguments": {
                        "source_ref": source_ref,
                        "cursor": next_read_cursor,
                        "max_chars": 20_000,
                    },
                    "repeat_until": "eof=true",
                },
            ],
            "next_action": (
                "Inspect every remaining structure page before defending partial reading, or continue sequential "
                "reading to EOF. Then recompute and retry the complete source analysis."
            ),
        }
    if partial_local_read and not verified_duplicate and not _structure_has_content_labels(structure):
        next_read_cursor = _next_sequential_read_cursor(
            workspace,
            set_id=set_id,
            source_ref=source_ref,
        )
        return {}, {
            "accepted": False,
            "error": "unstructured_source_requires_sequential_completion",
            "source_ref": source_ref,
            "read_progress": {
                "sequential_chars_read": int(ledger.get("sequential_chars_read") or 0),
                "total_chars": int(ledger.get("total_chars") or 0),
                "next_cursor": next_read_cursor,
            },
            "allowed_next_action": {
                "tool": "read_source",
                "arguments": {
                    "source_ref": source_ref,
                    "cursor": next_read_cursor,
                    "max_chars": 20_000,
                },
                "repeat_until": "eof=true",
            },
            "next_action": (
                "The document has no content-bearing section labels, so its unread regions cannot be assessed from "
                "structure metadata. Continue sequential reading to EOF, then recompute the source analysis."
            ),
        }
    structure_receipt_refs = sorted(
        {
            str(item.get("receipt_ref") or "")
            for item in workspace.get("source_structure_pages") or []
            if isinstance(item, dict)
            and str(item.get("set_id") or "") == set_id
            and str(item.get("source_ref") or "") == source_ref
            and str(item.get("delivery_status") or "") == "delivered"
            and str(item.get("receipt_ref") or "")
        }
    )
    return {
        "receipt_refs": receipt_refs,
        "structure_receipt_refs": structure_receipt_refs,
        "read_state_signature": source_read_state_signature(
            workspace,
            set_id=set_id,
            source_ref=source_ref,
            acknowledge_pending=True,
        ),
        **semantic_fields,
        "coverage_ledger": ledger,
        **(
            {"verified_exact_duplicate_of_source_ref": verified_duplicate}
            if verified_duplicate
            else {}
        ),
    }, None


def _structure_has_content_labels(structure: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("kind") or "") == "heading"
        and str(item.get("label_preview") or "").strip()
        for item in structure.get("items") or []
    )


def _next_sequential_read_cursor(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
) -> str:
    receipts = [
        item
        for item in workspace.get("source_read_receipts") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("source_ref") or "") == source_ref
        and str(item.get("read_kind") or "sequential") == "sequential"
        and source_read_receipt_is_delivered(item)
    ]
    if not receipts:
        return ""
    latest = max(receipts, key=lambda item: int(item.get("end_offset") or 0))
    return str(latest.get("receipt_ref") or "")


async def attach_evidence_previews(
    findings: list[dict[str, Any]],
    *,
    evidence_toolset: Any,
    preview_chars: int = 900,
) -> None:
    """Attach explicit compact previews; full evidence remains in EvidenceItem."""

    for finding in findings:
        previews: list[dict[str, Any]] = []
        for evidence_id in finding.get("evidence_ids") or []:
            evidence = await evidence_toolset._evidence_in_run(str(evidence_id))
            if evidence is None:
                continue
            snippet = str(getattr(evidence, "snippet_text", "") or "")
            preview = snippet[:preview_chars]
            previews.append(
                {
                    "evidence_id": str(evidence_id),
                    "snippet_preview": preview,
                    "snippet_chars": len(snippet),
                    "snippet_truncated": len(snippet) > len(preview),
                    "locator": str(getattr(evidence, "locator_text", "") or ""),
                }
            )
        finding["evidence_previews"] = previews


def reconcile_source_assessment_findings(
    assessment: dict[str, Any],
    *,
    findings: list[dict[str, Any]],
) -> bool:
    """Keep derived assessment links consistent with the source's current findings."""

    finding_ids = [
        str(finding.get("finding_id") or "")
        for finding in findings
        if str(finding.get("finding_id") or "")
    ]
    question_ids = list(
        dict.fromkeys(
            str(link.get("question_id") or "")
            for finding in findings
            for link in finding.get("question_links") or []
            if isinstance(link, dict) and str(link.get("question_id") or "")
        )
    )
    linked_question_ids = set(question_ids)
    dispositions = [
        item
        for item in assessment.get("question_dispositions") or []
        if isinstance(item, dict)
        and not (
            str(item.get("status") or "").lower() in {"supports", "contradicts"}
            and str(item.get("question_id") or "") not in linked_question_ids
        )
    ]
    changed = (
        assessment.get("finding_ids") != finding_ids
        or assessment.get("coverage_question_ids") != question_ids
        or assessment.get("question_dispositions") != dispositions
        or (bool(findings) and bool(str(assessment.get("no_findings_reason") or "").strip()))
    )
    assessment["finding_ids"] = finding_ids
    assessment["coverage_question_ids"] = question_ids
    assessment["question_dispositions"] = dispositions
    if findings:
        assessment["no_findings_reason"] = ""
    return changed


__all__ = [
    "attach_evidence_previews",
    "reconcile_source_assessment_findings",
    "validate_reading_basis",
]
