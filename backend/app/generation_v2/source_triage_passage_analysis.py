from __future__ import annotations

from typing import Any

from app.generation_v2.model_optional_fields import optional_model_text
from app.generation_v2.source_triage_exact_duplicates import (
    exact_duplicate_predecessor,
)
from app.generation_v2.source_triage_workspace_state import (
    source_read_receipt_is_delivered,
)
from app.generation_v2.source_triage_question_references import (
    possible_question_ids_error,
)
from app.generation_v2.tools.evidence_source_passage_handles import (
    accepted_passage_refs_for_receipt,
    validate_delivered_passage_refs,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context


def uses_passage_analysis_contract(args: dict[str, Any]) -> bool:
    return "source_summary" in args or any(
        isinstance(item, dict) and "passage_refs" in item
        for item in args.get("findings") or []
    )


def preserve_downstream_fields_for_unchanged_findings(
    findings: list[dict[str, Any]],
    *,
    previous_findings: list[dict[str, Any]],
) -> None:
    """Keep question links and relations when the source-owned finding did not change."""

    previous_by_id = {
        str(item.get("finding_id") or ""): item
        for item in previous_findings
        if str(item.get("finding_id") or "")
    }
    for finding in findings:
        previous = previous_by_id.get(str(finding.get("finding_id") or ""))
        if previous is None or _source_owned_finding(previous) != _source_owned_finding(finding):
            continue
        finding["question_links"] = [
            dict(item) for item in previous.get("question_links") or [] if isinstance(item, dict)
        ]
        finding["source_relations"] = [
            dict(item) for item in previous.get("source_relations") or [] if isinstance(item, dict)
        ]


def _source_owned_finding(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: finding.get(key)
        for key in (
            "finding_id",
            "set_id",
            "source_ref",
            "source_id",
            "source_version_ids",
            "assessment_id",
            "statement",
            "support_explanation",
            "evidence_ids",
            "passage_refs",
            "scope",
            "limitations",
            "possible_question_ids",
        )
    }


async def normalize_passage_analysis_args(
    owner: Any,
    *,
    workspace: dict[str, Any],
    source_set: dict[str, Any],
    source_ref: str,
    args: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Translate the set-agent contract into the durable legacy shape.

    Evidence is derived from passages that this agent actually received. Exact
    question links and source relations intentionally remain empty until the
    question workbench and finalizer stages respectively.
    """

    normalized = dict(args)
    representation_status = str(args.get("representation_status") or "unchecked")
    uniqueness = str(args.get("uniqueness") or "unknown")
    uniqueness_reason = str(args.get("uniqueness_reason") or "").strip()
    duplicate_of_source_ref = optional_model_text(args.get("duplicate_of_source_ref"))
    if uniqueness == "duplicate":
        if not duplicate_of_source_ref:
            return normalized, {
                "accepted": False,
                "error": "duplicate_source_ref_required",
                "next_action": "Set duplicate_of_source_ref to the earlier matching source_ref.",
            }
        if duplicate_of_source_ref == source_ref:
            return normalized, {
                "accepted": False,
                "error": "source_cannot_duplicate_itself",
            }
        if duplicate_of_source_ref not in {
            str(value) for value in source_set.get("source_refs") or []
        }:
            return normalized, {
                "accepted": False,
                "error": "duplicate_source_ref_outside_current_set",
                "duplicate_of_source_ref": duplicate_of_source_ref,
            }
        if not uniqueness_reason:
            return normalized, {
                "accepted": False,
                "error": "duplicate_requires_uniqueness_reason",
            }
    elif duplicate_of_source_ref:
        return normalized, {
            "accepted": False,
            "error": "duplicate_of_source_ref_requires_duplicate_uniqueness",
        }

    exact_duplicate = (
        await exact_duplicate_predecessor(
            owner.db,
            ref_context=build_source_ref_context(owner.inventory),
            ordered_source_refs=[str(value) for value in source_set.get("source_refs") or []],
            source_ref=source_ref,
        )
        if duplicate_of_source_ref
        else None
    )
    verified_exact_duplicate_ref = (
        duplicate_of_source_ref
        if exact_duplicate
        and duplicate_of_source_ref == str(exact_duplicate.get("source_ref") or "")
        else ""
    )
    normalized.update(
        {
            "processing_status": "resolved",
            "relevance": "unknown",
            "relevance_reason": "",
            "uniqueness": uniqueness,
            "uniqueness_reason": uniqueness_reason,
            "duplicate_of_source_ref": duplicate_of_source_ref,
            "verified_exact_duplicate_of_source_ref": verified_exact_duplicate_ref,
            "disposition": (
                "deferred"
                if representation_status in {"not_target_content", "unreadable"}
                else "background"
            ),
            "disposition_reason": (
                str(args.get("representation_reason") or "")
                if representation_status in {"not_target_content", "unreadable"}
                else ""
            ),
            "editorial_functions": [],
            "confidence": 0.5,
        }
    )
    normalized_findings: list[dict[str, Any]] = []
    all_evidence_ids: list[str] = []
    prepared_findings: list[
        tuple[dict[str, Any], list[str], list[str], list[str]]
    ] = []
    for finding_index, raw_finding in enumerate(args.get("findings") or []):
        if not isinstance(raw_finding, dict):
            return normalized, {
                "accepted": False,
                "error": "Each finding must be an object",
                "finding_index": finding_index,
            }
        passage_refs = _dedupe_strings(raw_finding.get("passage_refs"))
        explicit_evidence_ids = _dedupe_strings(raw_finding.get("evidence_ids"))
        if not passage_refs and not explicit_evidence_ids:
            return normalized, {
                "accepted": False,
                "error": "Each finding requires a read_source passage_ref or exact evidence_id",
                "finding_index": finding_index,
            }
        possible_question_ids = _dedupe_strings(raw_finding.get("possible_question_ids"))
        question_error = possible_question_ids_error(
            workspace,
            possible_question_ids,
            finding_index=finding_index,
        )
        if question_error:
            return normalized, {**question_error, "source_ref": source_ref}
        if passage_refs:
            _available_refs, passage_error = validate_delivered_passage_refs(
                workspace,
                set_id=str(source_set.get("set_id") or ""),
                source_ref=source_ref,
                passage_refs=passage_refs,
                allowed_read_kinds={"sequential"},
            )
            if passage_error:
                return normalized, {**passage_error, "finding_index": finding_index}
        prepared_findings.append(
            (
                raw_finding,
                passage_refs,
                explicit_evidence_ids,
                possible_question_ids,
            )
        )

    evidence_toolset = owner.evidence_toolset
    if prepared_findings and evidence_toolset is None:
        return normalized, {
            "accepted": False,
            "error": "passage_evidence_materialization_unavailable",
        }

    for finding_index, (_, _, explicit_evidence_ids, _) in enumerate(
        prepared_findings
    ):
        evidence_error = await _validate_explicit_evidence_ids(
            evidence_toolset,
            source_ref=source_ref,
            evidence_ids=explicit_evidence_ids,
        )
        if evidence_error:
            return normalized, {**evidence_error, "finding_index": finding_index}

    for finding_index, (
        raw_finding,
        passage_refs,
        explicit_evidence_ids,
        possible_question_ids,
    ) in enumerate(prepared_findings):
        materialized_evidence_ids: list[str] = []
        if passage_refs:
            materialized = await evidence_toolset.materialize_source_passages(
                source_ref=source_ref,
                passage_refs=passage_refs,
                allowed_read_kinds={"sequential"},
            )
            if materialized.get("accepted") is not True:
                return normalized, {**materialized, "finding_index": finding_index}
            materialized_evidence_ids = [
                str(value) for value in materialized.get("evidence_ids") or []
            ]
        evidence_ids = list(
            dict.fromkeys([*explicit_evidence_ids, *materialized_evidence_ids])
        )
        all_evidence_ids.extend(evidence_ids)
        normalized_findings.append(
            {
                **raw_finding,
                "passage_refs": passage_refs,
                "possible_question_ids": possible_question_ids,
                "evidence_ids": evidence_ids,
                "question_links": [],
                "source_relations": [],
            }
        )

    if not normalized_findings and representation_status in {
        "readable",
        "partial",
        "not_target_content",
    }:
        delivered_passage_refs = _delivered_passage_refs(
            workspace,
            source_set=source_set,
            source_ref=source_ref,
        )
        if delivered_passage_refs and evidence_toolset is not None:
            materialized = await evidence_toolset.materialize_source_passages(
                source_ref=source_ref,
                passage_refs=[delivered_passage_refs[0]],
            )
            if materialized.get("accepted") is not True:
                return normalized, materialized
            all_evidence_ids.extend(
                str(value) for value in materialized.get("evidence_ids") or []
            )

    normalized["findings"] = normalized_findings
    normalized["evidence_ids"] = list(dict.fromkeys(all_evidence_ids))
    derived_relevance, derived_disposition = _derived_source_card_axes(
        representation_status=representation_status,
        findings=normalized_findings,
    )
    normalized["relevance"] = derived_relevance
    normalized["disposition"] = derived_disposition
    return normalized, None


def _derived_source_card_axes(
    *,
    representation_status: str,
    findings: list[dict[str, Any]],
) -> tuple[str, str]:
    """Derive redundant source-card axes from durable facts instead of asking the model twice."""

    if representation_status in {"not_target_content", "unreadable"}:
        return "unknown", "deferred"
    if any(finding.get("possible_question_ids") for finding in findings):
        return "supporting", "supporting"
    if findings:
        return "contextual", "background"
    return "out_of_scope", "background"


async def _validate_explicit_evidence_ids(
    evidence_toolset: Any,
    *,
    source_ref: str,
    evidence_ids: list[str],
) -> dict[str, Any] | None:
    if not evidence_ids:
        return None
    evidence_loader = getattr(evidence_toolset, "_evidence_in_run", None)
    source_ref_loader = getattr(evidence_toolset, "_source_ref_for_id", None)
    exact_read_checker = getattr(
        evidence_toolset,
        "_evidence_has_current_exact_read",
        None,
    )
    if not callable(evidence_loader) or not callable(source_ref_loader) or not callable(
        exact_read_checker
    ):
        return {
            "accepted": False,
            "error": "exact_evidence_validation_unavailable",
        }
    invalid: list[str] = []
    not_exact: list[str] = []
    for evidence_id in evidence_ids:
        evidence = await evidence_loader(evidence_id)
        if evidence is None:
            invalid.append(evidence_id)
            continue
        evidence_source_ref = await source_ref_loader(evidence.source_id)
        if evidence_source_ref != source_ref:
            invalid.append(evidence_id)
            continue
        if not exact_read_checker(evidence):
            not_exact.append(evidence_id)
    if invalid:
        return {
            "accepted": False,
            "error": "finding_evidence_must_belong_to_current_source_and_run",
            "invalid_evidence_ids": invalid,
        }
    if not_exact:
        return {
            "accepted": False,
            "error": "finding_evidence_requires_current_exact_read",
            "not_exact_read_evidence_ids": not_exact,
            "next_action": (
                "Call read_source_window for the selected source location, then use the returned evidence_id."
            ),
        }
    return None


def _delivered_passage_refs(
    workspace: dict[str, Any],
    *,
    source_set: dict[str, Any],
    source_ref: str,
) -> list[str]:
    set_id = str(source_set.get("set_id") or "")
    return list(
        dict.fromkeys(
            str(passage_ref)
            for receipt in workspace.get("source_read_receipts") or []
            if isinstance(receipt, dict)
            and str(receipt.get("set_id") or "") == set_id
            and str(receipt.get("source_ref") or "") == source_ref
            and source_read_receipt_is_delivered(receipt)
            for passage_ref in accepted_passage_refs_for_receipt(receipt)
            if str(passage_ref).strip()
        )
    )


def _dedupe_strings(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values or [] if str(value).strip()))


__all__ = [
    "normalize_passage_analysis_args",
    "preserve_downstream_fields_for_unchanged_findings",
    "uses_passage_analysis_contract",
]
