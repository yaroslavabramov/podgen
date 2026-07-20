from __future__ import annotations

import json
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.source_triage_read_checkpointing import (
    pending_comparison_source_refs,
    pending_exact_read_source_refs,
    source_refs_with_reads_newer_than_analysis,
)


def pending_set_work(workspace: dict[str, Any], source_set: dict[str, Any]) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    assessed_refs = {
        str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "resolved") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
    }
    completed_question_ids = {
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("status") or "") in {"covered", "partial", "missing", "conflicted", "deferred"}
        and not bool(item.get("is_stale"))
    }
    required_question_ids = [str(item) for item in source_set.get("question_ids") or []]
    stale_question_ids = [
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id and bool(item.get("is_stale"))
    ]
    set_findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_reading_notes = [
        item
        for item in workspace.get("reading_notes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    return {
        "pending_source_refs": [
            str(item) for item in source_set.get("source_refs") or [] if str(item) not in assessed_refs
        ],
        "required_question_ids": required_question_ids,
        "missing_outcome_question_ids": [
            question_id for question_id in required_question_ids if question_id not in completed_question_ids
        ],
        "stale_outcome_question_ids": workspace_api._dedupe(stale_question_ids),
        "pending_exact_read_source_refs": pending_exact_read_source_refs(
            workspace,
            source_set=source_set,
        ),
        "pending_comparison_source_refs": pending_comparison_source_refs(source_set),
        "stale_source_analysis_refs": source_refs_with_reads_newer_than_analysis(
            workspace,
            set_id=set_id,
        ),
        "saved_state": {
            "source_refs": sorted(assessed_refs),
            "finding_ids": sorted(
                str(item.get("finding_id") or "") for item in set_findings if str(item.get("finding_id") or "")
            ),
            "outcome_question_ids": sorted(
                str(item.get("question_id") or "") for item in set_outcomes if str(item.get("question_id") or "")
            ),
            "reading_note_ids": sorted(
                str(item.get("note_id") or "") for item in set_reading_notes if str(item.get("note_id") or "")
            ),
            "reading_note_count": len(set_reading_notes),
            "source_analysis_count": len(assessed_refs),
            "finding_count": len(set_findings),
            "outcome_count": len(set_outcomes),
        },
    }


def confidence(value: Any) -> float:
    return max(0.0, min(float(0.5 if value is None else value), 1.0))


def semantic_json(value: Any) -> str:
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items() if key not in {"updated_at", "semantic_revision"}}
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def finding_supports_question(finding: dict[str, Any], question_id: str) -> bool:
    return any(
        isinstance(link, dict) and str(link.get("question_id") or "") == question_id
        for link in finding.get("question_links") or []
    )


def outcome_argument_finding_ids(args: dict[str, Any]) -> list[str]:
    return workspace_api._dedupe(
        [
            *[str(item) for item in args.get("finding_ids") or [] if str(item)],
            *[
                str(finding_id)
                for result in args.get("requirement_results") or []
                if isinstance(result, dict)
                for finding_id in result.get("finding_ids") or []
                if str(finding_id)
            ],
        ]
    )


def outcome_argument_requirement_ids(args: dict[str, Any]) -> list[str]:
    return workspace_api._dedupe(
        [
            str(result.get("requirement_id") or "")
            for result in args.get("requirement_results") or []
            if isinstance(result, dict) and str(result.get("requirement_id") or "")
        ]
    )


def outcome_argument_lineage_errors(
    *,
    args: dict[str, Any],
    allowed_finding_ids: set[str],
    findings_by_id: dict[str, dict[str, Any]],
    set_id: str,
    question_id: str,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    requirement_finding_ids: set[str] = set()
    for result in args.get("requirement_results") or []:
        if not isinstance(result, dict):
            continue
        requirement_id = str(result.get("requirement_id") or "")
        finding_ids = [str(item) for item in result.get("finding_ids") or [] if str(item) in allowed_finding_ids]
        requirement_finding_ids.update(finding_ids)
        errors.extend(
            finding_lineage_errors(
                finding_ids=finding_ids,
                findings_by_id=findings_by_id,
                set_id=set_id,
                question_id=question_id,
                requirement_id=requirement_id or None,
            )
        )
    top_level_only = [
        finding_id
        for finding_id in outcome_argument_finding_ids(args)
        if finding_id in allowed_finding_ids and finding_id not in requirement_finding_ids
    ]
    errors.extend(
        finding_lineage_errors(
            finding_ids=top_level_only,
            findings_by_id=findings_by_id,
            set_id=set_id,
            question_id=question_id,
        )
    )
    return errors


def finding_lineage_errors(
    *,
    finding_ids: list[str],
    findings_by_id: dict[str, dict[str, Any]],
    set_id: str | None,
    question_id: str,
    requirement_id: str | None = None,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for finding_id in finding_ids:
        finding = findings_by_id.get(finding_id)
        if finding is None:
            errors.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement_id or "",
                    "finding_id": finding_id,
                    "source_ref": "",
                    "reason": "unknown_finding",
                    "corrective_action": "Page saved source_findings and copy an exact current finding_id.",
                }
            )
            continue
        source_ref = str(finding.get("source_ref") or "")
        finding_set_id = str(finding.get("set_id") or "")
        if set_id is not None and finding_set_id != set_id:
            errors.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement_id or "",
                    "finding_id": finding_id,
                    "source_ref": source_ref,
                    "finding_set_id": finding_set_id,
                    "required_set_id": set_id,
                    "reason": "finding_from_another_set",
                    "corrective_action": "Use a finding owned by the current set; do not copy cross-set lineage.",
                }
            )
            continue
        linked = (
            finding_supports_requirement(finding, question_id, requirement_id)
            if requirement_id
            else finding_supports_question(finding, question_id)
        )
        if not linked:
            errors.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement_id or "",
                    "finding_id": finding_id,
                    "source_ref": source_ref,
                    "reason": (
                        "finding_not_linked_to_requirement" if requirement_id else "finding_not_linked_to_question"
                    ),
                    "corrective_action": (
                        "Resave this source's complete analysis with the finding linked to the exact question and "
                        "requirement; otherwise remove this finding from the outcome."
                    ),
                }
            )
    return errors


def lineage_repair_next_action(lineage_errors: list[dict[str, Any]]) -> str:
    if not lineage_errors:
        return "Retry with the exact current-set findings linked to this question requirement."
    if any(str(item.get("reason") or "").startswith("finding_not_linked") for item in lineage_errors):
        return (
            "For each listed source_ref, resave save_source_analysis with the finding's exact question_id and "
            "requirement_ids, then retry; otherwise remove the offending finding_id from this outcome."
        )
    if any(item.get("reason") == "unknown_finding" for item in lineage_errors):
        return "Page the current saved state, copy exact finding_ids, and retry."
    return "Replace findings from another set with linked findings owned by this set, then retry."


def finding_supports_requirement(
    finding: dict[str, Any],
    question_id: str,
    requirement_id: str,
) -> bool:
    return any(
        isinstance(link, dict)
        and str(link.get("question_id") or "") == question_id
        and requirement_id in {str(item) for item in link.get("requirement_ids") or []}
        for link in finding.get("question_links") or []
    )


def local_requirement_ids(
    *,
    question: dict[str, Any],
    source_set: dict[str, Any],
    findings_by_id: dict[str, dict[str, Any]],
    question_id: str,
) -> set[str]:
    known_ids = {
        str(item.get("requirement_id") or "")
        for item in question.get("answer_requirements") or []
        if isinstance(item, dict) and str(item.get("requirement_id") or "")
    }
    assigned_ids: set[str] = set()
    assignment_found = False
    for assignment in source_set.get("question_assignments") or []:
        if not isinstance(assignment, dict):
            continue
        if str(assignment.get("question_id") or "") != question_id:
            continue
        assignment_found = True
        assigned_ids.update(str(value) for value in assignment.get("requirement_ids") or [] if str(value))
    planned_question_ids = {
        str(value) for value in source_set.get("planned_question_ids", source_set.get("question_ids") or [])
    }
    if not assignment_found and question_id in planned_question_ids:
        # Completed V2 work before explicit requirement assignments treated a
        # planned question as whole-question responsibility.
        assigned_ids.update(known_ids)
    set_id = str(source_set.get("set_id") or "")
    linked_ids = {
        str(requirement_id)
        for finding in findings_by_id.values()
        if str(finding.get("set_id") or "") == set_id
        for link in finding.get("question_links") or []
        if isinstance(link, dict) and str(link.get("question_id") or "") == question_id
        for requirement_id in link.get("requirement_ids") or []
        if str(requirement_id)
    }
    return (assigned_ids | linked_ids) & known_ids


def requirement_status_consistency_error(
    *,
    overall_status: str,
    requirement_statuses: list[str],
) -> dict[str, Any] | None:
    statuses = set(requirement_statuses)
    consistent = {
        "covered": statuses == {"covered"},
        "missing": statuses == {"missing"},
        "deferred": bool(statuses) and "deferred" in statuses and statuses.issubset({"missing", "deferred"}),
        "conflicted": "conflicted" in statuses,
        "partial": (
            bool(statuses)
            and "conflicted" not in statuses
            and not statuses.issubset({"covered"})
            and not statuses.issubset({"missing"})
            and not ("deferred" in statuses and statuses.issubset({"missing", "deferred"}))
        ),
    }.get(overall_status, False)
    if consistent:
        return None
    return {
        "accepted": False,
        "error": "Overall coverage status contradicts saved requirement results",
        "overall_status": overall_status,
        "requirement_statuses": requirement_statuses,
        "next_action": "Correct the semantic result statuses or the overall status and retry.",
    }


def question_next_action_from_requirements(
    *,
    requirement_results: list[dict[str, Any]],
    fallback: str,
) -> str:
    """Aggregate structured routes while preserving the legacy question-level contract."""

    if not requirement_results:
        return fallback
    actions = {str(item.get("next_action") or "none") for item in requirement_results}
    for action in ("source_acquisition", "research", "omit", "none"):
        if action in actions:
            return action
    return "none"


__all__ = [
    "confidence",
    "finding_lineage_errors",
    "finding_supports_question",
    "finding_supports_requirement",
    "lineage_repair_next_action",
    "local_requirement_ids",
    "outcome_argument_finding_ids",
    "outcome_argument_lineage_errors",
    "outcome_argument_requirement_ids",
    "pending_set_work",
    "question_next_action_from_requirements",
    "requirement_status_consistency_error",
    "semantic_json",
]
