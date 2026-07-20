from __future__ import annotations

from typing import Any

from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS
from app.generation_v2.source_triage_workspace_state import (
    source_read_coverage_ledger,
    source_read_receipt_is_delivered,
)


def validate_reading_basis(
    *,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    source_ref: str,
    representation_status: str,
    raw_basis: Any,
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
    if representation_status in {"readable", "partial"} and not receipt_refs:
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
    ledger = next(
        (
            item
            for item in source_read_coverage_ledger(
                list(workspace.get("source_read_receipts") or []),
                set_id=set_id,
            )
            if item.get("source_ref") == source_ref
        ),
        {
            "source_ref": source_ref,
            "total_chars": 0,
            "sequential_ranges": [],
            "sequential_chars_read": 0,
            "unread_char_count": 0,
            "eof_reached": False,
            "search_ranges": [],
            "search_queries": [],
            "structure": {},
            "completion_is_model_judgment": True,
        },
    )
    return {
        "receipt_refs": receipt_refs,
        **semantic_fields,
        "coverage_ledger": ledger,
    }, None


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
