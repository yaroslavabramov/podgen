from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from app.generation_v2.corpus_brief_actions import outcome_routes_to_research
from app.generation_v2.contracts import SourceCardCoverageStatus
from app.generation_v2.schemas import (
    CorpusBrief,
    CorpusBriefAuditIssue,
    CorpusBriefAuditReport,
    CorpusBriefIntent,
    CorpusBriefSourceSet,
    CoverageOutcome,
    CoverageQuestion,
    ResearchHandoffItem,
    SourceAssessment,
    SourceCard,
    SourceMap,
    SourceTriageItem,
)
from app.generation_v2.tools.evidence_source_map_ids import source_map_evidence_ids


FINAL_OUTCOME_STATUSES = {"covered", "partial", "missing", "conflicted", "deferred"}
FINAL_SET_STATUSES = {"mapped", "deferred"}

_DISPOSITION_PRIORITY = {
    "anchor": 4,
    "supporting": 3,
    "background": 2,
    "weak": 1,
    "unusable": 0,
    "deferred": 0,
}
_RELEVANCE_PRIORITY = {
    "core": 4,
    "supporting": 3,
    "contextual": 2,
    "unknown": 1,
    "out_of_scope": 0,
}
_AUTHORITY_PRIORITY = {
    "primary": 7,
    "official": 6,
    "expert": 5,
    "secondary": 4,
    "tertiary": 3,
    "weak": 2,
    "unknown": 1,
}
_UNIQUENESS_PRIORITY = {
    "unique": 4,
    "complementary": 3,
    "derivative": 2,
    "duplicate": 1,
    "unknown": 0,
}


def _assessment_priority(assessment: SourceAssessment) -> tuple[int, int, int, int]:
    return (
        _DISPOSITION_PRIORITY.get(str(assessment.disposition), 0),
        _RELEVANCE_PRIORITY.get(str(assessment.relevance), 0),
        _AUTHORITY_PRIORITY.get(str(assessment.authority), 0),
        _UNIQUENESS_PRIORITY.get(str(assessment.uniqueness), 0),
    )


def _assessment_score(assessment: SourceAssessment) -> float:
    """Encode the four lexicographic assessment axes as readable decimal places."""

    disposition, relevance, authority, uniqueness = _assessment_priority(assessment)
    return float(disposition * 1_000 + relevance * 100 + authority * 10 + uniqueness)


def coverage_question_material_lineage(
    workspace: dict[str, Any],
    question_id: str,
) -> dict[str, list[str]]:
    """Return the persisted source/evidence material actually attached to one question."""

    assessments = [
        item for item in workspace.get("source_assessments") or [] if isinstance(item, dict)
    ]
    source_ref_by_id = {
        str(item.get("source_id") or ""): str(item.get("source_ref") or "")
        for item in assessments
        if item.get("source_id") and item.get("source_ref")
    }
    assigned_refs = {
        str(source_ref)
        for source_set in workspace.get("source_sets") or []
        if isinstance(source_set, dict)
        and question_id in {str(value) for value in source_set.get("question_ids") or []}
        for source_ref in source_set.get("source_refs") or []
    }
    linked_assessments = [
        item
        for item in assessments
        if question_id in {str(value) for value in item.get("coverage_question_ids") or []}
        or (
            "coverage_question_ids" not in item
            and str(item.get("source_ref") or "") in assigned_refs
        )
    ]
    linked_assessment_refs = {
        str(item.get("source_ref") or "") for item in linked_assessments
    }
    question_outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
    ]
    outcome_refs = {
        *[
            str(source_ref)
            for outcome in question_outcomes
            for source_ref in outcome.get("source_refs") or []
        ],
        *[
            source_ref_by_id.get(str(source_id), "")
            for outcome in question_outcomes
            for source_id in outcome.get("source_ids") or []
        ],
    }
    allowed_ref_set = {ref for ref in linked_assessment_refs | outcome_refs if ref}
    source_order = [str(item) for item in workspace.get("source_order") or []]
    source_refs = [ref for ref in source_order if ref in allowed_ref_set]
    source_refs.extend(sorted(allowed_ref_set - set(source_refs)))
    evidence_ids = _dedupe(
        [
            *[
                str(evidence_id)
                for assessment in linked_assessments
                for evidence_id in assessment.get("evidence_ids") or []
            ],
            *[
                str(evidence_id)
                for outcome in question_outcomes
                for evidence_id in outcome.get("evidence_ids") or []
            ],
        ]
    )
    return {"source_refs": source_refs, "evidence_ids": evidence_ids}


def coverage_synthesis_input_hash(workspace: dict[str, Any], question_id: str) -> str:
    """Hash the durable question state that a run-wide synthesis is derived from."""

    assigned_sets = [
        item
        for item in workspace.get("source_sets") or []
        if isinstance(item, dict) and question_id in {str(value) for value in item.get("question_ids") or []}
    ]
    outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
    ]
    material_lineage = coverage_question_material_lineage(workspace, question_id)
    assigned_refs = {
        str(source_ref)
        for source_set in assigned_sets
        for source_ref in source_set.get("source_refs") or []
    }
    linked_assessments = [
        item
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and (
            question_id in {str(value) for value in item.get("coverage_question_ids") or []}
            or (
                "coverage_question_ids" not in item
                and str(item.get("source_ref") or "") in assigned_refs
            )
        )
    ]
    payload = {
        "semantic_revision": int(workspace.get("triage_semantic_revision") or 0),
        "question": next(
            (
                item
                for item in workspace.get("coverage_questions") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            None,
        ),
        "material_lineage": material_lineage,
        "source_sets": sorted(
            (_without_volatile_fields(item) for item in assigned_sets),
            key=lambda item: str(item.get("set_id") or ""),
        ),
        "coverage_outcomes": sorted(
            (_without_volatile_fields(item) for item in outcomes),
            key=lambda item: (str(item.get("set_id") or ""), str(item.get("outcome_id") or "")),
        ),
        "source_assessments": sorted(
            (_without_volatile_fields(item) for item in linked_assessments),
            key=lambda item: str(item.get("source_ref") or ""),
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _without_volatile_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"created_at", "updated_at", "started_at"}}


def canonical_workspace_violations(
    workspace: dict[str, Any],
    *,
    require_syntheses: bool = False,
) -> list[dict[str, Any]]:
    """Return objective consistency failures only; do not judge editorial quality."""

    source_order = [str(item) for item in workspace.get("source_order") or []]
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    source_sets = [item for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    assessments = [item for item in workspace.get("source_assessments") or [] if isinstance(item, dict)]
    outcomes = [item for item in workspace.get("coverage_outcomes") or [] if isinstance(item, dict)]
    violations: list[dict[str, Any]] = []

    question_ids = [str(item.get("question_id") or "") for item in questions]
    if not question_ids:
        violations.append({"code": "missing_coverage_questions", "message": "No coverage questions were persisted."})
    elif any(not question_id for question_id in question_ids):
        violations.append(
            {
                "code": "invalid_coverage_question_id",
                "message": "Every coverage question needs a non-empty id.",
            }
        )
    duplicate_question_ids = _duplicates(question_ids)
    if duplicate_question_ids:
        violations.append(
            {
                "code": "duplicate_coverage_questions",
                "message": "Coverage question ids must be unique.",
                "question_ids": duplicate_question_ids,
            }
        )
    known_question_ids = {question_id for question_id in question_ids if question_id}
    unknown_parent_ids = sorted(
        {
            str(item.get("parent_question_id") or "")
            for item in questions
            if item.get("parent_question_id") and str(item.get("parent_question_id")) not in known_question_ids
        }
    )
    if unknown_parent_ids:
        violations.append(
            {
                "code": "unknown_parent_coverage_questions",
                "message": "A coverage question references an unknown parent question.",
                "question_ids": unknown_parent_ids,
            }
        )

    set_ids = [str(item.get("set_id") or "") for item in source_sets]
    if any(not set_id for set_id in set_ids):
        violations.append(
            {
                "code": "invalid_source_set_id",
                "message": "Every source set needs a non-empty id.",
            }
        )
    duplicate_set_ids = _duplicates(set_ids)
    if duplicate_set_ids:
        violations.append(
            {
                "code": "duplicate_source_sets",
                "message": "Source set ids must be unique.",
                "set_ids": duplicate_set_ids,
            }
        )
    set_ref_counts: dict[str, int] = defaultdict(int)
    assigned_question_ids: set[str] = set()
    unknown_set_question_ids: set[str] = set()
    for source_set in source_sets:
        for source_ref in source_set.get("source_refs") or []:
            set_ref_counts[str(source_ref)] += 1
        for question_id in source_set.get("question_ids") or []:
            question_id = str(question_id)
            assigned_question_ids.add(question_id)
            if question_id not in known_question_ids:
                unknown_set_question_ids.add(question_id)
        if str(source_set.get("status") or "") not in FINAL_SET_STATUSES:
            violations.append(
                {
                    "code": "unfinished_source_set",
                    "message": "A source set is not durably finished.",
                    "set_id": str(source_set.get("set_id") or ""),
                }
            )
        if (
            str(source_set.get("status") or "") == "deferred"
            and not str(source_set.get("deferred_reason") or "").strip()
        ):
            violations.append(
                {
                    "code": "unexplained_deferred_source_set",
                    "message": "A deferred source set needs an explicit reason.",
                    "set_id": str(source_set.get("set_id") or ""),
                }
            )
        if (
            str(source_set.get("status") or "") == "mapped"
            and str(source_set.get("deferred_reason") or "").strip()
        ):
            violations.append(
                {
                    "code": "inconsistent_source_set_status",
                    "message": "A mapped source set must keep its set-level deferred reason empty.",
                    "set_id": str(source_set.get("set_id") or ""),
                }
            )
    if unknown_set_question_ids:
        violations.append(
            {
                "code": "unknown_set_coverage_questions",
                "message": "A source set references unknown coverage questions.",
                "question_ids": sorted(unknown_set_question_ids),
            }
        )
    unassigned_question_ids = sorted(known_question_ids - assigned_question_ids)
    if unassigned_question_ids:
        violations.append(
            {
                "code": "unassigned_coverage_questions",
                "message": "Every coverage question must be assigned to at least one source set.",
                "question_ids": unassigned_question_ids,
            }
        )
    missing_set_refs = [ref for ref in source_order if set_ref_counts.get(ref, 0) == 0]
    duplicate_set_refs = [ref for ref in source_order if set_ref_counts.get(ref, 0) > 1]
    unknown_set_refs = sorted(set(set_ref_counts) - set(source_order))
    if missing_set_refs:
        violations.append(
            {
                "code": "unassigned_sources",
                "message": "Every source must belong to exactly one source set.",
                "source_refs": missing_set_refs,
            }
        )
    if duplicate_set_refs:
        violations.append(
            {
                "code": "multiply_assigned_sources",
                "message": "A source cannot belong to more than one source set.",
                "source_refs": duplicate_set_refs,
            }
        )
    if unknown_set_refs:
        violations.append(
            {
                "code": "unknown_source_refs",
                "message": "A source set references sources outside the current corpus.",
                "source_refs": unknown_set_refs,
            }
        )

    assessments_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assessment in assessments:
        assessments_by_ref[str(assessment.get("source_ref") or "")].append(assessment)
    missing_assessment_refs = [ref for ref in source_order if not assessments_by_ref.get(ref)]
    duplicate_assessment_refs = [ref for ref in source_order if len(assessments_by_ref.get(ref) or []) > 1]
    unknown_assessment_refs = sorted(set(assessments_by_ref) - set(source_order))
    if missing_assessment_refs:
        violations.append(
            {
                "code": "unassessed_sources",
                "message": "Every source needs one persisted assessment.",
                "source_refs": missing_assessment_refs,
            }
        )
    if duplicate_assessment_refs:
        violations.append(
            {
                "code": "duplicate_source_assessments",
                "message": "Every source needs exactly one current assessment.",
                "source_refs": duplicate_assessment_refs,
            }
        )
    if unknown_assessment_refs:
        violations.append(
            {
                "code": "unknown_source_assessments",
                "message": "A source assessment references a source outside the current corpus.",
                "source_refs": unknown_assessment_refs,
            }
        )
    unfinished_assessment_refs = [
        ref
        for ref, values in assessments_by_ref.items()
        if values and str(values[-1].get("processing_status") or "") != "resolved"
    ]
    if unfinished_assessment_refs:
        violations.append(
            {
                "code": "unfinished_source_assessments",
                "message": "Partial processing cannot be finalized; finish or explicitly resolve the source assessment.",
                "source_refs": unfinished_assessment_refs,
            }
        )
    unchecked_representation_refs = sorted(
        ref
        for ref, values in assessments_by_ref.items()
        if values and str(values[-1].get("representation_status") or "unchecked") == "unchecked"
    )
    if unchecked_representation_refs:
        violations.append(
            {
                "code": "unchecked_source_representations",
                "message": "Each source representation must be checked or explicitly marked unreadable.",
                "source_refs": unchecked_representation_refs,
            }
        )
    set_id_by_source_ref = {
        str(source_ref): str(source_set.get("set_id") or "")
        for source_set in source_sets
        for source_ref in source_set.get("source_refs") or []
    }
    assessment_set_mismatches = sorted(
        {
            str(assessment.get("source_ref") or "")
            for assessment in assessments
            if str(assessment.get("source_ref") or "") in set_id_by_source_ref
            and str(assessment.get("set_id") or "") != set_id_by_source_ref[str(assessment.get("source_ref") or "")]
        }
    )
    if assessment_set_mismatches:
        violations.append(
            {
                "code": "source_assessment_set_mismatch",
                "message": "A source assessment must belong to the source's assigned set.",
                "source_refs": assessment_set_mismatches,
            }
        )

    current_assessment_by_ref = {
        source_ref: values[-1] for source_ref, values in assessments_by_ref.items() if source_ref and values
    }
    assessment_deferred_refs = {
        source_ref
        for source_ref, assessment in current_assessment_by_ref.items()
        if str(assessment.get("disposition") or "") == "deferred"
    }
    contradictory_active_assessment_refs = sorted(
        source_ref
        for source_ref, assessment in current_assessment_by_ref.items()
        if (
            str(assessment.get("disposition") or "") in {"anchor", "supporting"}
            and str(assessment.get("relevance") or "unknown") == "out_of_scope"
        )
        or (
            str(assessment.get("representation_status") or "unchecked") == "unreadable"
            and str(assessment.get("disposition") or "") != "deferred"
        )
        or (
            str(assessment.get("disposition") or "") == "deferred"
            and str(assessment.get("representation_status") or "unchecked") not in {"partial", "unreadable"}
        )
        or (
            str(assessment.get("disposition") or "") in {"weak", "unusable", "deferred"}
            and not str(assessment.get("disposition_reason") or "").strip()
        )
    )
    if contradictory_active_assessment_refs:
        violations.append(
            {
                "code": "inconsistent_source_assessment",
                "message": (
                    "Assessment representation, disposition, and disposition_reason must remain independent and coherent."
                ),
                "source_refs": contradictory_active_assessment_refs,
            }
        )
    indexed_deferred_reasons = {
        str(item.get("source_ref") or ""): str(item.get("reason") or "").strip()
        for item in workspace.get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
    }
    indexed_deferred_refs = set(indexed_deferred_reasons)
    invalid_deferred_index_refs = {
        *[source_ref for source_ref in indexed_deferred_refs if source_ref not in assessment_deferred_refs],
        *[source_ref for source_ref in assessment_deferred_refs if not indexed_deferred_reasons.get(source_ref)],
    }
    if invalid_deferred_index_refs:
        violations.append(
            {
                "code": "deferred_source_index_mismatch",
                "message": "Deferred source state must match the source assessment and include a concrete reason.",
                "source_refs": sorted(invalid_deferred_index_refs),
            }
        )
    deferred_set_assessment_refs = sorted(
        {
            str(source_ref)
            for source_set in source_sets
            if str(source_set.get("status") or "") == "deferred"
            for source_ref in source_set.get("source_refs") or []
            if str(source_ref) not in assessment_deferred_refs
        }
    )
    if deferred_set_assessment_refs:
        violations.append(
            {
                "code": "deferred_set_assessment_mismatch",
                "message": "Every source in a deferred set must have disposition=deferred.",
                "source_refs": deferred_set_assessment_refs,
            }
        )

    expected_outcomes = {
        (str(source_set.get("set_id") or ""), str(question_id))
        for source_set in source_sets
        for question_id in source_set.get("question_ids") or []
    }
    final_outcomes = {
        (str(item.get("set_id") or ""), str(item.get("question_id") or ""))
        for item in outcomes
        if str(item.get("status") or "") in FINAL_OUTCOME_STATUSES
    }
    all_outcome_pairs = [(str(item.get("set_id") or ""), str(item.get("question_id") or "")) for item in outcomes]
    duplicate_outcome_pairs = sorted(pair for pair, count in _counts(all_outcome_pairs).items() if count > 1)
    if duplicate_outcome_pairs:
        violations.append(
            {
                "code": "duplicate_coverage_outcomes",
                "message": "Each source-set/question pair needs exactly one current outcome.",
                "set_question_pairs": [list(item) for item in duplicate_outcome_pairs],
            }
        )
    unexpected_outcome_pairs = sorted(set(all_outcome_pairs) - expected_outcomes)
    if unexpected_outcome_pairs:
        violations.append(
            {
                "code": "unexpected_coverage_outcomes",
                "message": "A coverage outcome must belong to an assigned source-set/question pair.",
                "set_question_pairs": [list(item) for item in unexpected_outcome_pairs],
            }
        )
    missing_outcomes = sorted(expected_outcomes - final_outcomes)
    if missing_outcomes:
        violations.append(
            {
                "code": "unresolved_coverage_questions",
                "message": "Every question assigned to a source set needs a final outcome.",
                "set_question_pairs": [list(item) for item in missing_outcomes],
            }
        )
    unknown_outcome_question_ids = sorted(
        {
            str(item.get("question_id") or "")
            for item in outcomes
            if str(item.get("question_id") or "") not in known_question_ids
        }
    )
    if unknown_outcome_question_ids:
        violations.append(
            {
                "code": "unknown_coverage_questions",
                "message": "Coverage outcomes reference unknown questions.",
                "question_ids": unknown_outcome_question_ids,
            }
        )
    source_ref_by_id = {
        str(assessment.get("source_id") or ""): source_ref
        for source_ref, assessment in current_assessment_by_ref.items()
        if str(assessment.get("source_id") or "").strip()
    }
    active_outcomes_using_deferred: list[list[str]] = []
    active_outcome_deferred_refs: set[str] = set()
    for item in outcomes:
        if str(item.get("status") or "") not in {"covered", "partial", "conflicted"}:
            continue
        outcome_refs = {str(value) for value in item.get("source_refs") or []}
        outcome_refs.update(
            source_ref_by_id[str(source_id)]
            for source_id in item.get("source_ids") or []
            if str(source_id) in source_ref_by_id
        )
        invalid_refs = outcome_refs.intersection(assessment_deferred_refs)
        if not invalid_refs:
            continue
        active_outcomes_using_deferred.append([str(item.get("set_id") or ""), str(item.get("question_id") or "")])
        active_outcome_deferred_refs.update(invalid_refs)
    if active_outcomes_using_deferred:
        violations.append(
            {
                "code": "deferred_source_active_outcome",
                "message": "Covered, partial, or conflicted outcomes cannot use a deferred source.",
                "source_refs": sorted(active_outcome_deferred_refs),
                "set_question_pairs": active_outcomes_using_deferred,
            }
        )
    assessment_ineligible_refs = {
        source_ref for source_ref, assessment in current_assessment_by_ref.items() if not research_eligible(assessment)
    }
    active_outcomes_using_ineligible: list[list[str]] = []
    active_outcome_ineligible_refs: set[str] = set()
    for item in outcomes:
        if str(item.get("status") or "") not in {"covered", "partial", "conflicted"}:
            continue
        outcome_refs = {str(value) for value in item.get("source_refs") or []}
        outcome_refs.update(
            source_ref_by_id[str(source_id)]
            for source_id in item.get("source_ids") or []
            if str(source_id) in source_ref_by_id
        )
        invalid_refs = outcome_refs.intersection(assessment_ineligible_refs - assessment_deferred_refs)
        if not invalid_refs:
            continue
        active_outcomes_using_ineligible.append([str(item.get("set_id") or ""), str(item.get("question_id") or "")])
        active_outcome_ineligible_refs.update(invalid_refs)
    if active_outcomes_using_ineligible:
        violations.append(
            {
                "code": "ineligible_source_active_outcome",
                "message": (
                    "Covered, partial, or conflicted outcomes cannot use unreadable, out-of-scope, or unusable sources."
                ),
                "source_refs": sorted(active_outcome_ineligible_refs),
                "set_question_pairs": active_outcomes_using_ineligible,
            }
        )
    deferred_set_ids = {
        str(source_set.get("set_id") or "")
        for source_set in source_sets
        if str(source_set.get("status") or "") == "deferred"
    }
    deferred_set_active_outcomes = sorted(
        [
            str(item.get("set_id") or ""),
            str(item.get("question_id") or ""),
        ]
        for item in outcomes
        if str(item.get("set_id") or "") in deferred_set_ids and str(item.get("status") or "") != "deferred"
    )
    if deferred_set_active_outcomes:
        violations.append(
            {
                "code": "deferred_set_active_outcome",
                "message": "Every coverage outcome in a deferred set must be explicitly deferred.",
                "set_question_pairs": deferred_set_active_outcomes,
            }
        )
    incomplete_outcomes: list[list[str]] = []
    source_set_by_id = {
        str(item.get("set_id") or ""): item
        for item in source_sets
        if str(item.get("set_id") or "")
    }
    for item in outcomes:
        status = str(item.get("status") or "")
        orientation_summary = str(item.get("orientation_summary") or "").strip()
        missing_detail = str(item.get("missing_detail") or "").strip()
        conflict_summary = str(item.get("conflict_summary") or "").strip()
        deferred_reason = str(item.get("deferred_reason") or "").strip()
        source_refs = _dedupe([str(value) for value in item.get("source_refs") or []])
        source_ids = _dedupe([str(value) for value in item.get("source_ids") or []])
        expected_source_ids = _dedupe(
            [
                str((current_assessment_by_ref.get(source_ref) or {}).get("source_id") or "")
                for source_ref in source_refs
            ]
        )
        valid = True
        if status == "missing":
            valid = bool(missing_detail) and not orientation_summary and not conflict_summary and not deferred_reason
        elif status == "conflicted":
            valid = bool(source_ids and item.get("evidence_ids") and conflict_summary) and not deferred_reason
        elif status == "deferred":
            relevant_refs = source_refs or _dedupe(
                [
                    str(value)
                    for value in (
                        source_set_by_id.get(str(item.get("set_id") or ""), {}).get("source_refs") or []
                    )
                ]
            )
            has_representation_blocker = any(
                str((current_assessment_by_ref.get(source_ref) or {}).get("representation_status") or "")
                in {"partial", "unreadable"}
                for source_ref in relevant_refs
            )
            valid = (
                bool(deferred_reason)
                and not orientation_summary
                and not missing_detail
                and not conflict_summary
                and has_representation_blocker
            )
        elif status == "covered":
            valid = bool(
                item.get("source_ids")
                and item.get("evidence_ids")
                and orientation_summary
                and not missing_detail
                and not deferred_reason
            )
        elif status == "partial":
            valid = bool(
                source_ids
                and item.get("evidence_ids")
                and
                orientation_summary
                and missing_detail
                and not deferred_reason
            )
        if source_ids != expected_source_ids:
            valid = False
        if not valid:
            incomplete_outcomes.append([str(item.get("set_id") or ""), str(item.get("question_id") or "")])
    if incomplete_outcomes:
        violations.append(
            {
                "code": "incomplete_coverage_outcome",
                "message": "A final coverage outcome is missing its required explanation or grounding.",
                "set_question_pairs": incomplete_outcomes,
            }
        )
    if require_syntheses:
        violations.extend(_coverage_synthesis_violations(workspace))
    return violations


def _coverage_synthesis_violations(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate only concrete structure of the run-wide question synthesis."""

    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    syntheses = [item for item in workspace.get("coverage_syntheses") or [] if isinstance(item, dict)]
    question_ids = [str(item.get("question_id") or "") for item in questions if item.get("question_id")]
    known_question_ids = set(question_ids)
    synthesis_question_ids = [str(item.get("question_id") or "") for item in syntheses]
    violations: list[dict[str, Any]] = []

    duplicate_ids = _duplicates(synthesis_question_ids)
    if duplicate_ids:
        violations.append(
            {
                "code": "duplicate_coverage_syntheses",
                "message": "Every coverage question needs exactly one run-wide synthesis.",
                "question_ids": duplicate_ids,
            }
        )
    missing_ids = sorted(known_question_ids - set(synthesis_question_ids))
    if missing_ids:
        violations.append(
            {
                "code": "missing_coverage_syntheses",
                "message": "Every coverage question needs a run-wide synthesis across all assigned source sets.",
                "question_ids": missing_ids,
            }
        )
    unknown_ids = sorted(set(synthesis_question_ids) - known_question_ids)
    if unknown_ids:
        violations.append(
            {
                "code": "unknown_coverage_syntheses",
                "message": "A coverage synthesis references an unknown question.",
                "question_ids": unknown_ids,
            }
        )

    assessments_by_ref = {
        str(item.get("source_ref") or ""): item
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
    }
    source_id_by_ref = {
        source_ref: str(item.get("source_id") or "")
        for source_ref, item in assessments_by_ref.items()
        if str(item.get("source_id") or "").strip()
    }
    known_source_refs = {str(item) for item in workspace.get("source_order") or []}
    persisted_evidence_refs: dict[str, set[str]] = defaultdict(set)
    for item in workspace.get("source_assessments") or []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        for evidence_id in item.get("evidence_ids") or []:
            if str(evidence_id).strip() and source_ref:
                persisted_evidence_refs[str(evidence_id)].add(source_ref)
    source_ref_by_id = {source_id: source_ref for source_ref, source_id in source_id_by_ref.items()}
    for item in workspace.get("coverage_outcomes") or []:
        if not isinstance(item, dict):
            continue
        outcome_refs = {
            *[str(value) for value in item.get("source_refs") or [] if str(value).strip()],
            *[
                source_ref_by_id[str(source_id)]
                for source_id in item.get("source_ids") or []
                if source_ref_by_id.get(str(source_id))
            ],
        }
        for evidence_id in item.get("evidence_ids") or []:
            evidence_id = str(evidence_id)
            if evidence_id.strip() and evidence_id not in persisted_evidence_refs:
                # An assessment binds evidence to one exact source. Only fall
                # back to the outcome's declared refs when no exact binding is
                # available in the canonical workspace.
                persisted_evidence_refs[evidence_id].update(outcome_refs)
    persisted_evidence_ids = set(persisted_evidence_refs)
    invalid_syntheses: list[str] = []
    invalid_source_refs: set[str] = set()
    invalid_evidence_ids: set[str] = set()
    current_input_revision = int(workspace.get("triage_semantic_revision") or 0)
    source_set_by_id = {
        str(item.get("set_id") or ""): item
        for item in workspace.get("source_sets") or []
        if isinstance(item, dict) and str(item.get("set_id") or "")
    }
    deferred_outcome_question_ids: set[str] = set()
    for outcome in workspace.get("coverage_outcomes") or []:
        if not isinstance(outcome, dict) or str(outcome.get("status") or "") != "deferred":
            continue
        relevant_refs = _dedupe(
            [str(value) for value in outcome.get("source_refs") or []]
        ) or _dedupe(
            [
                str(value)
                for value in (
                    source_set_by_id.get(str(outcome.get("set_id") or ""), {}).get("source_refs") or []
                )
            ]
        )
        if any(
            str((assessments_by_ref.get(source_ref) or {}).get("representation_status") or "")
            in {"partial", "unreadable"}
            for source_ref in relevant_refs
        ):
            deferred_outcome_question_ids.add(str(outcome.get("question_id") or ""))
    for item in syntheses:
        question_id = str(item.get("question_id") or "")
        status = str(item.get("status") or "")
        source_refs = _dedupe([str(value) for value in item.get("source_refs") or []])
        source_ids = _dedupe([str(value) for value in item.get("source_ids") or []])
        evidence_ids = _dedupe([str(value) for value in item.get("evidence_ids") or []])
        orientation_summary = str(item.get("orientation_summary") or "").strip()
        missing_detail = str(item.get("missing_detail") or "").strip()
        conflict_summary = str(item.get("conflict_summary") or "").strip()
        deferred_reason = str(item.get("deferred_reason") or "").strip()
        valid = status in FINAL_OUTCOME_STATUSES
        if status in {"covered", "partial", "conflicted"}:
            valid = valid and bool(source_refs and evidence_ids)
        if status == "covered":
            valid = valid and bool(orientation_summary) and not missing_detail and not deferred_reason
        elif status == "partial":
            valid = valid and bool(orientation_summary and missing_detail) and not deferred_reason
        elif status == "missing":
            valid = (
                valid
                and bool(missing_detail)
                and not orientation_summary
                and not conflict_summary
                and not deferred_reason
            )
        elif status == "conflicted":
            valid = valid and bool(conflict_summary) and not deferred_reason
        elif status == "deferred":
            valid = (
                valid
                and bool(deferred_reason)
                and not orientation_summary
                and not missing_detail
                and not conflict_summary
                and question_id in deferred_outcome_question_ids
            )
        if str(item.get("next_action") or "") not in {"research", "source_acquisition", "omit", "none"}:
            valid = False
        if int(item.get("input_revision") or 0) != current_input_revision:
            valid = False
        if str(item.get("input_hash") or "") != coverage_synthesis_input_hash(workspace, question_id):
            valid = False
        if source_ids != _dedupe([source_id_by_ref.get(ref, "") for ref in source_refs]):
            valid = False
        material_lineage = coverage_question_material_lineage(workspace, question_id)
        unknown_refs = set(source_refs) - known_source_refs
        unlinked_refs = set(source_refs) - set(material_lineage["source_refs"])
        ineligible_refs = {
            ref
            for ref in source_refs
            if status in {"covered", "partial", "conflicted"}
            and (ref not in assessments_by_ref or not research_eligible(assessments_by_ref[ref]))
        }
        stale_evidence = set(evidence_ids) - persisted_evidence_ids
        unlinked_evidence = set(evidence_ids) - set(material_lineage["evidence_ids"])
        misbound_evidence = {
            evidence_id
            for evidence_id in evidence_ids
            if evidence_id in persisted_evidence_refs
            and not persisted_evidence_refs[evidence_id].intersection(source_refs)
        }
        invalid_source_refs.update(unknown_refs | unlinked_refs | ineligible_refs)
        invalid_evidence_ids.update(stale_evidence | unlinked_evidence | misbound_evidence)
        if (
            not valid
            or unknown_refs
            or unlinked_refs
            or ineligible_refs
            or stale_evidence
            or unlinked_evidence
            or misbound_evidence
        ):
            invalid_syntheses.append(question_id)
    if invalid_syntheses:
        violations.append(
            {
                "code": "invalid_coverage_synthesis",
                "message": (
                    "A run-wide synthesis has an inconsistent status payload or references ineligible/unreviewed material."
                ),
                "question_ids": sorted(set(invalid_syntheses)),
                "source_refs": sorted(invalid_source_refs),
                "evidence_ids": sorted(invalid_evidence_ids),
            }
        )
    return violations


def research_eligible(assessment: dict[str, Any] | SourceAssessment) -> bool:
    value = assessment.model_dump(mode="json") if isinstance(assessment, SourceAssessment) else assessment
    return bool(
        str(value.get("processing_status") or "resolved") == "resolved"
        and str(value.get("representation_status") or "unchecked") in {"readable", "partial"}
        and str(value.get("relevance") or "unknown") != "out_of_scope"
        and str(value.get("disposition") or "background") not in {"unusable", "deferred"}
    )


def handoff_evidence_ids(
    outcome: CoverageOutcome,
    *,
    preferred_source_ids: list[str],
    assessment_by_source: dict[str, SourceAssessment],
    evidence_ids_by_source: dict[str, set[str]],
) -> list[str]:
    """Keep validated outcome evidence without requiring duplicate assessment writes."""

    declared_source_ids = set(outcome.source_ids)
    eligible_source_ids = set(preferred_source_ids)
    ineligible_source_ids = declared_source_ids - eligible_source_ids
    known_eligible_evidence = {
        evidence_id
        for source_id in eligible_source_ids
        for evidence_id in {
            *(assessment_by_source[source_id].evidence_ids if source_id in assessment_by_source else []),
            *evidence_ids_by_source.get(source_id, set()),
        }
    }
    known_ineligible_evidence = {
        evidence_id
        for source_id in ineligible_source_ids
        for evidence_id in {
            *(assessment_by_source[source_id].evidence_ids if source_id in assessment_by_source else []),
            *evidence_ids_by_source.get(source_id, set()),
        }
    }
    all_declared_sources_are_eligible = bool(declared_source_ids) and declared_source_ids <= eligible_source_ids
    return [
        evidence_id
        for evidence_id in outcome.evidence_ids
        if evidence_id not in known_ineligible_evidence
        and (evidence_id in known_eligible_evidence or all_declared_sources_are_eligible)
    ]


def materialize_source_map_v2(
    source_map: SourceMap,
    *,
    workspace: dict[str, Any],
    inventory: SourceMap,
) -> SourceMap:
    """Make the workspace, not trace frequency, the canonical triage state."""

    assessments = [
        SourceAssessment.model_validate(item)
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
    ]
    assessment_by_source = {item.source_id: item for item in assessments}
    cards_by_source = {card.source_id: card.model_copy(deep=True) for card in source_map.source_cards}
    inventory_by_source = {card.source_id: card for card in inventory.source_cards}
    source_ids_by_ref = {
        str(item.get("source_ref") or ""): str(item.get("source_id") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
    }
    ordered_source_ids = [source_ids_by_ref.get(str(ref), "") for ref in workspace.get("source_order") or []]
    ordered_source_ids = [source_id for source_id in ordered_source_ids if source_id]
    deferred_source_refs = {
        str(item.get("source_ref") or "")
        for item in workspace.get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    }

    cards: list[SourceCard] = []
    ranked_candidates: list[tuple[int, SourceAssessment, SourceTriageItem]] = []
    top_sources_by_function: dict[str, list[str]] = defaultdict(list)
    top_evidence_by_function: dict[str, list[str]] = defaultdict(list)
    all_evidence_ids = list(source_map.evidence_ids)

    for corpus_index, source_id in enumerate(ordered_source_ids):
        assessment = assessment_by_source.get(source_id)
        base = cards_by_source.get(source_id) or inventory_by_source.get(source_id)
        if assessment is None or base is None:
            continue
        card = base.model_copy(deep=True)
        card.source_version_ids = list(assessment.source_version_ids or card.source_version_ids)
        card.source_profile_id = assessment.source_profile_id
        card.coverage_question_ids = list(assessment.coverage_question_ids)
        card.editorial_functions = list(assessment.editorial_functions)
        card.evidence_ids = _dedupe([*card.evidence_ids, *assessment.evidence_ids])
        card.authority = str(assessment.authority)
        card.useful_for = _dedupe([*card.useful_for, *assessment.editorial_functions])
        card.gaps_or_cautions = _dedupe([*card.gaps_or_cautions, *assessment.limitations])
        raw_assessment = next(
            (
                item
                for item in workspace.get("source_assessments") or []
                if isinstance(item, dict) and str(item.get("source_id") or "") == source_id
            ),
            {},
        )
        representation_status = str(raw_assessment.get("representation_status") or "unchecked")
        disposition = str(assessment.disposition)
        if representation_status == "unreadable" or disposition == "unusable":
            card.coverage_status = SourceCardCoverageStatus.FAILED
        elif representation_status == "partial" or disposition in {"weak", "deferred"}:
            card.coverage_status = SourceCardCoverageStatus.PARTIAL
        else:
            card.coverage_status = SourceCardCoverageStatus.READY
        cards.append(card)
        all_evidence_ids.extend(card.evidence_ids)
        relevance_reasons = [assessment.relevance_reason] if assessment.relevance_reason else []
        source_ref = str(raw_assessment.get("source_ref") or "")
        if research_eligible(raw_assessment) and source_ref not in deferred_source_refs:
            ranked_candidates.append(
                (
                    corpus_index,
                    assessment,
                    SourceTriageItem(
                        source_id=source_id,
                        source_version_ids=card.source_version_ids,
                        evidence_ids=card.evidence_ids,
                        rank=0,
                        score=_assessment_score(assessment),
                        authority=str(assessment.authority),
                        reliability=card.reliability,
                        useful_for=card.useful_for,
                        top_quote_refs=[
                            quote.evidence_id or "" for quote in source_map.quote_cards if quote.source_id == source_id
                        ],
                        relevance_reasons=relevance_reasons,
                        cautions=card.gaps_or_cautions,
                    ),
                )
            )

    ranked_candidates.sort(
        key=lambda candidate: (
            *(-value for value in _assessment_priority(candidate[1])),
            candidate[0],
        )
    )
    ranked = [item.model_copy(update={"rank": rank}) for rank, (_, _, item) in enumerate(ranked_candidates, 1)]
    materialized_cards_by_source = {card.source_id: card for card in cards}
    for item in ranked:
        card = materialized_cards_by_source[item.source_id]
        for function in card.editorial_functions or ["background"]:
            top_sources_by_function[function].append(item.source_id)
            top_evidence_by_function[function].extend(card.evidence_ids)

    triage = source_map.source_triage.model_copy(deep=True)
    triage.ranked_sources = ranked
    triage.top_sources_by_function = {key: _dedupe(value) for key, value in top_sources_by_function.items()}
    triage.top_evidence_by_function = {key: _dedupe(value) for key, value in top_evidence_by_function.items()}
    return source_map.model_copy(
        update={
            "schema_version": "source_map.v2",
            "source_cards": cards,
            "source_triage": triage,
            "source_assessments": assessments,
            "evidence_ids": _dedupe(all_evidence_ids),
            "retrieval_notes": _dedupe(
                [
                    *source_map.retrieval_notes,
                    "Ranked sources use disposition, relevance, authority, and uniqueness in that order, with corpus order as the stable tie-break; score encodes those four axes as decimal places and never uses evidence count.",
                ]
            ),
        }
    )


def aggregate_coverage_outcomes(workspace: dict[str, Any]) -> list[CoverageOutcome]:
    questions = [
        item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)
    ]
    syntheses = [
        item for item in workspace.get("coverage_syntheses") or [] if isinstance(item, dict)
    ]
    synthesis_by_question = {
        str(item.get("question_id") or ""): item
        for item in syntheses
        if str(item.get("question_id") or "").strip()
    }
    question_ids = [str(item.get("question_id") or "") for item in questions]
    complete_syntheses = (
        question_ids
        and len(syntheses) == len(question_ids)
        and set(synthesis_by_question) == set(question_ids)
    )
    fresh_syntheses = complete_syntheses and all(
        int(synthesis_by_question[question_id].get("input_revision") or 0)
        == int(workspace.get("triage_semantic_revision") or 0)
        and str(synthesis_by_question[question_id].get("input_hash") or "")
        == coverage_synthesis_input_hash(workspace, question_id)
        for question_id in question_ids
    )
    if fresh_syntheses:
        return [
            CoverageOutcome.model_validate(synthesis_by_question[question_id])
            for question_id in question_ids
        ]
    if syntheses:
        raise ValueError("Run-wide coverage syntheses are incomplete or stale; finalization must rebuild them.")

    # Compatibility for completed workspaces created before run-wide
    # question synthesis existed. New finalization requires syntheses.
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in workspace.get("coverage_outcomes") or []:
        if isinstance(item, dict):
            grouped[str(item.get("question_id") or "")].append(item)
    results: list[CoverageOutcome] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("question_id") or "")
        values = grouped.get(question_id, [])
        statuses = {str(item.get("status") or "unknown") for item in values}
        if "conflicted" in statuses:
            status = "conflicted"
        elif statuses == {"covered"}:
            status = "covered"
        elif statuses == {"missing"}:
            status = "missing"
        elif statuses == {"deferred"}:
            status = "deferred"
        elif statuses:
            status = "partial"
        else:
            status = "unknown"
        next_actions = {str(item.get("next_action") or "research") for item in values}
        next_action = next(
            (
                candidate
                for candidate in ["source_acquisition", "research", "omit", "none"]
                if candidate in next_actions
            ),
            "research",
        )
        results.append(
            CoverageOutcome(
                outcome_id=f"outcome:{question_id}",
                question_id=question_id,
                status=status,
                source_ids=_dedupe([str(source_id) for item in values for source_id in item.get("source_ids") or []]),
                evidence_ids=_dedupe(
                    [str(evidence_id) for item in values for evidence_id in item.get("evidence_ids") or []]
                ),
                orientation_summary=_join_semantic(values, "orientation_summary"),
                missing_detail=_join_semantic(values, "missing_detail"),
                conflict_summary=_join_semantic(values, "conflict_summary"),
                deferred_reason=_join_semantic(values, "deferred_reason"),
                confidence=min(
                    [
                        float(0.5 if item.get("confidence") is None else item.get("confidence"))
                        for item in values
                    ]
                    or [0.5]
                ),
                next_action=next_action,
            )
        )
    return results


def materialize_corpus_brief(
    *,
    generation_run_id: str,
    project_id: str,
    run_corpus_view_id: str,
    corpus_revision: int,
    attempt: int,
    source_map_artifact_id: str,
    source_map: SourceMap,
    workspace: dict[str, Any],
) -> CorpusBrief:
    questions = [
        CoverageQuestion.model_validate(item)
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict)
    ]
    outcomes = aggregate_coverage_outcomes(workspace)
    assessment_by_source = {item.source_id: item for item in source_map.source_assessments}
    evidence_ids_by_source = {card.source_id: set(card.evidence_ids) for card in source_map.source_cards}
    source_id_by_ref = {
        str(item.get("source_ref") or ""): str(item.get("source_id") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
    }
    deferred_source_refs = {
        str(item.get("source_ref") or "")
        for item in workspace.get("deferred_sources") or []
        if isinstance(item, dict) and str(item.get("reason") or "").strip()
    }
    deferred_source_refs.update(
        str(source_ref)
        for source_set in workspace.get("source_sets") or []
        if isinstance(source_set, dict) and str(source_set.get("status") or "") == "deferred"
        for source_ref in source_set.get("source_refs") or []
    )
    deferred_source_ids = {
        source_id_by_ref[source_ref] for source_ref in deferred_source_refs if source_id_by_ref.get(source_ref)
    }
    source_sets: list[CorpusBriefSourceSet] = []
    for item in workspace.get("source_sets") or []:
        if not isinstance(item, dict):
            continue
        source_ids = _dedupe([source_id_by_ref.get(str(ref), "") for ref in item.get("source_refs") or []])
        source_sets.append(
            CorpusBriefSourceSet(
                set_id=str(item.get("set_id") or ""),
                label=str(item.get("label") or ""),
                purpose=str(item.get("purpose") or ""),
                rationale=str(item.get("rationale") or ""),
                source_ids=source_ids,
                question_ids=[str(value) for value in item.get("question_ids") or []],
                priority=str(item.get("priority") or "normal"),
                status=str(item.get("status") or "mapped"),
                findings_summary=str(item.get("findings_summary") or ""),
                evidence_ids=[str(value) for value in item.get("evidence_ids") or []],
                deferred_reason=str(item.get("deferred_reason") or ""),
            )
        )

    question_by_id = {item.question_id: item for item in questions}
    handoff: list[ResearchHandoffItem] = []
    for outcome in outcomes:
        if str(outcome.status) == "deferred" or not outcome_routes_to_research(outcome):
            continue
        preferred_source_ids = [
            source_id
            for source_id in outcome.source_ids
            if source_id in assessment_by_source
            and source_id not in deferred_source_ids
            and research_eligible(assessment_by_source[source_id])
        ]
        if outcome.source_ids and not preferred_source_ids:
            # A synthesis that cites only deferred/ineligible sources is not
            # converted into an empty active handoff. A genuine run-wide gap
            # has no source_ids and can still be handed off for research.
            continue
        question = question_by_id.get(outcome.question_id)
        if str(outcome.status) == "conflicted":
            action = "compare"
        elif str(outcome.status) == "missing":
            action = "close_gap"
        elif str(outcome.status) == "partial":
            action = "corroborate"
        else:
            action = "deep_read"
        handoff.append(
            ResearchHandoffItem(
                handoff_id=f"handoff:{outcome.question_id}",
                priority=str(getattr(question, "importance", "supporting")),
                objective=str(getattr(question, "question", outcome.question_id)),
                question_ids=[outcome.question_id],
                preferred_source_ids=preferred_source_ids,
                starting_evidence_ids=handoff_evidence_ids(
                    outcome,
                    preferred_source_ids=preferred_source_ids,
                    assessment_by_source=assessment_by_source,
                    evidence_ids_by_source=evidence_ids_by_source,
                ),
                action=action,
                reason=(
                    outcome.conflict_summary
                    or outcome.missing_detail
                    or outcome.orientation_summary
                    or outcome.deferred_reason
                ),
            )
        )

    intent_payload = dict(workspace.get("intent") or {})
    central_question = next(
        (item.question for item in questions if str(item.importance) == "critical"),
        questions[0].question if questions else str(intent_payload.get("user_prompt") or ""),
    )
    cautions = _dedupe(
        [
            str(gap)
            for gap in [
                *source_map.gaps,
                *source_map.source_triage.gaps,
                *source_map.contradictions,
                *source_map.source_triage.contradictions,
                *(record.description for record in source_map.gap_records),
            ]
            if str(gap)
        ]
        + [
            detail
            for outcome in outcomes
            for detail in [outcome.missing_detail, outcome.conflict_summary, outcome.deferred_reason]
            if detail
        ]
    )
    source_map_payload = source_map.model_dump(mode="json")
    source_map_hash = hashlib.sha256(
        json.dumps(source_map_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CorpusBrief(
        generation_run_id=generation_run_id,
        project_id=project_id,
        run_corpus_view_id=run_corpus_view_id,
        corpus_revision=corpus_revision,
        attempt=attempt,
        source_map_artifact_id=source_map_artifact_id,
        source_map_hash=source_map_hash,
        intent=CorpusBriefIntent(
            user_prompt=str(intent_payload.get("user_prompt") or ""),
            central_question=central_question,
            language=str(intent_payload.get("language") or ""),
            duration_seconds=int(intent_payload.get("duration_seconds") or 0) or None,
            constraints=[str(item) for item in intent_payload.get("constraints") or []],
        ),
        coverage_questions=questions,
        source_sets=source_sets,
        coverage_outcomes=outcomes,
        research_handoff=handoff,
        gap_records=[item.model_copy(deep=True) for item in source_map.gap_records],
        summary=str(
            (workspace.get("source_map_finalization") or {}).get("summary") or workspace.get("plan_summary") or ""
        ),
        cautions=cautions,
    )


def audit_corpus_brief(
    *,
    corpus_brief_artifact_id: str,
    brief: CorpusBrief,
    source_map: SourceMap,
    selected_source_ids: set[str],
    expected_generation_run_id: str,
    expected_project_id: str,
    expected_run_corpus_view_id: str,
    expected_corpus_revision: int,
    expected_attempt: int,
    expected_source_map_artifact_id: str,
) -> CorpusBriefAuditReport:
    """Audit identities and completed states; semantic adequacy remains a shadow audit."""

    issues: list[CorpusBriefAuditIssue] = []
    valid_evidence_ids = set(source_map_evidence_ids(source_map))
    valid_question_ids = {item.question_id for item in brief.coverage_questions}
    assessment_by_source = {item.source_id: item for item in source_map.source_assessments}
    evidence_ids_by_source = {card.source_id: set(card.evidence_ids) for card in source_map.source_cards}
    scope_mismatches = [
        label
        for label, actual, expected in (
            ("generation_run_id", brief.generation_run_id, expected_generation_run_id),
            ("project_id", brief.project_id, expected_project_id),
            ("run_corpus_view_id", brief.run_corpus_view_id, expected_run_corpus_view_id),
            ("corpus_revision", brief.corpus_revision, expected_corpus_revision),
            ("attempt", brief.attempt, expected_attempt),
            ("source_map_artifact_id", brief.source_map_artifact_id, expected_source_map_artifact_id),
        )
        if actual != expected
    ]
    if scope_mismatches:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:scope_identity",
                code="inconsistent_scope",
                severity="blocking",
                message=f"CorpusBrief scope does not match the current run: {', '.join(scope_mismatches)}.",
                suggested_repair="Rebuild CorpusBrief from the current run, corpus revision, attempt, and SourceMap.",
            )
        )
    referenced_source_ids = {
        *(source_id for source_set in brief.source_sets for source_id in source_set.source_ids),
        *(source_id for outcome in brief.coverage_outcomes for source_id in outcome.source_ids),
        *(source_id for item in brief.research_handoff for source_id in item.preferred_source_ids),
        *(source_id for item in brief.gap_records for source_id in item.source_ids),
    }
    referenced_evidence_ids = {
        *(evidence_id for source_set in brief.source_sets for evidence_id in source_set.evidence_ids),
        *(evidence_id for outcome in brief.coverage_outcomes for evidence_id in outcome.evidence_ids),
        *(evidence_id for item in brief.research_handoff for evidence_id in item.starting_evidence_ids),
        *(evidence_id for item in brief.gap_records for evidence_id in item.evidence_ids),
    }
    invalid_source_ids = sorted(referenced_source_ids - selected_source_ids)
    invalid_evidence_ids = sorted(referenced_evidence_ids - valid_evidence_ids)
    if invalid_source_ids or invalid_evidence_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:invalid_reference",
                code="invalid_reference",
                severity="blocking",
                message="CorpusBrief references identities outside the current run/source map.",
                source_ids=invalid_source_ids,
                evidence_ids=invalid_evidence_ids,
                suggested_repair="Rebuild the brief from the current workspace and SourceMap.",
            )
        )
    expected_gap_records = [item.model_dump(mode="json") for item in source_map.gap_records]
    actual_gap_records = [item.model_dump(mode="json") for item in brief.gap_records]
    if actual_gap_records != expected_gap_records:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:gap_lineage",
                code="unsupported_coverage",
                severity="blocking",
                message="CorpusBrief must preserve the SourceMap gap lineage without dropping or changing records.",
                suggested_repair="Rebuild CorpusBrief from the current SourceMap gap records.",
            )
        )
    source_set_source_ids = {source_id for source_set in brief.source_sets for source_id in source_set.source_ids}
    missing_source_set_ids = sorted(selected_source_ids - source_set_source_ids)
    if missing_source_set_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:unresolved_source",
                code="unresolved_source",
                severity="blocking",
                message="Every selected source must remain represented in a CorpusBrief source set.",
                source_ids=missing_source_set_ids,
                suggested_repair="Rebuild source sets from the completed triage workspace.",
            )
        )
    outcome_question_ids = {item.question_id for item in brief.coverage_outcomes}
    unresolved_question_ids = sorted(valid_question_ids - outcome_question_ids)
    unresolved_question_ids.extend(
        item.question_id for item in brief.coverage_outcomes if str(item.status) == "unknown"
    )
    unresolved_question_ids = sorted(set(unresolved_question_ids))
    if unresolved_question_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:unresolved_question",
                code="unresolved_question",
                severity="blocking",
                message="Every coverage question must have a final explicit outcome.",
                question_ids=unresolved_question_ids,
                suggested_repair="Finish or explicitly defer each unresolved coverage question.",
            )
        )
    deferred_set_source_ids = {
        source_id
        for source_set in brief.source_sets
        if str(source_set.status) == "deferred"
        for source_id in source_set.source_ids
    }
    ineligible_handoff_source_ids = sorted(
        {
            source_id
            for item in brief.research_handoff
            for source_id in item.preferred_source_ids
            if source_id in deferred_set_source_ids
            or source_id not in assessment_by_source
            or not research_eligible(assessment_by_source[source_id])
        }
    )
    if ineligible_handoff_source_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:deferred_source_in_handoff",
                code="deferred_source_in_handoff",
                severity="blocking",
                message="Unusable, unreadable, out-of-scope, or deferred sources cannot be active research inputs.",
                source_ids=ineligible_handoff_source_ids,
                suggested_repair="Remove those sources from active handoff and retain their cautions/gaps.",
            )
        )
    outcome_by_question = {item.question_id: item for item in brief.coverage_outcomes}
    invalid_handoff_evidence_ids: set[str] = set()
    for item in brief.research_handoff:
        allowed_evidence_ids = {
            evidence_id
            for question_id in item.question_ids
            if (outcome := outcome_by_question.get(question_id)) is not None
            for evidence_id in handoff_evidence_ids(
                outcome,
                preferred_source_ids=item.preferred_source_ids,
                assessment_by_source=assessment_by_source,
                evidence_ids_by_source=evidence_ids_by_source,
            )
        }
        invalid_handoff_evidence_ids.update(set(item.starting_evidence_ids) - allowed_evidence_ids)
    if invalid_handoff_evidence_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:ineligible_evidence_in_handoff",
                code="deferred_source_in_handoff",
                severity="blocking",
                message="Research handoff evidence must belong to an eligible finalized coverage outcome.",
                evidence_ids=sorted(invalid_handoff_evidence_ids),
                suggested_repair="Rebuild the handoff from the validated per-question coverage outcomes.",
            )
        )
    expected_handoff_question_ids = {
        outcome.question_id
        for outcome in brief.coverage_outcomes
        if str(outcome.status) != "deferred"
        and outcome_routes_to_research(outcome)
        and (
            not outcome.source_ids
            or any(
                source_id in assessment_by_source
                and source_id not in deferred_set_source_ids
                and research_eligible(assessment_by_source[source_id])
                for source_id in outcome.source_ids
            )
        )
    }
    actual_handoff_question_ids = [question_id for item in brief.research_handoff for question_id in item.question_ids]
    mismatched_handoff_question_ids = sorted(
        expected_handoff_question_ids.symmetric_difference(actual_handoff_question_ids)
        | set(_duplicates(actual_handoff_question_ids))
    )
    for item in brief.research_handoff:
        for question_id in item.question_ids:
            outcome = outcome_by_question.get(question_id)
            if outcome is None:
                continue
            if set(item.preferred_source_ids) - set(outcome.source_ids):
                mismatched_handoff_question_ids.append(question_id)
            if set(item.starting_evidence_ids) - set(outcome.evidence_ids):
                mismatched_handoff_question_ids.append(question_id)
    mismatched_handoff_question_ids = sorted(set(mismatched_handoff_question_ids))
    if mismatched_handoff_question_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:research_handoff",
                code="unsupported_coverage",
                severity="blocking",
                message="Research handoff must match the final per-question coverage outcomes.",
                question_ids=mismatched_handoff_question_ids,
                suggested_repair="Regenerate research handoff deterministically from coverage outcomes.",
            )
        )
    expected_hash = hashlib.sha256(
        json.dumps(
            source_map.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if brief.source_map_hash != expected_hash:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:source_map_hash",
                code="inconsistent_scope",
                severity="blocking",
                message="CorpusBrief was not built from the referenced current SourceMap payload.",
                suggested_repair="Rebuild CorpusBrief from the current SourceMap artifact.",
            )
        )
    return CorpusBriefAuditReport(
        corpus_brief_artifact_id=corpus_brief_artifact_id,
        source_map_artifact_id=brief.source_map_artifact_id,
        passed=not issues,
        reported_passed=not issues,
        summary=(
            "CorpusBrief objective audit passed."
            if not issues
            else "CorpusBrief objective audit found blocking consistency errors."
        ),
        issues=issues,
        checked_source_count=len(referenced_source_ids),
        checked_question_count=len(valid_question_ids),
        checked_evidence_count=len(referenced_evidence_ids),
    )


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return sorted(duplicate)


def _counts(values: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return counts


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _join_semantic(values: list[dict[str, Any]], field: str) -> str:
    return "\n\n".join(
        _dedupe([str(item.get(field) or "").strip() for item in values if str(item.get(field) or "").strip()])
    )


__all__ = [
    "aggregate_coverage_outcomes",
    "audit_corpus_brief",
    "canonical_workspace_violations",
    "materialize_corpus_brief",
    "materialize_source_map_v2",
    "research_eligible",
]
