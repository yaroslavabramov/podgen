from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from app.generation_v2.corpus_brief_actions import (
    outcome_routes_to_research,
    requirement_ids_routed_to_research,
)
from app.generation_v2.source_triage_workspace_state import (
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
    TRIAGE_WORKFLOW_SOURCE_MAPPING,
    is_structured_triage_workspace,
    stable_set_id,
    triage_workflow_mode,
)
from app.generation_v2.source_triage_research_handoff import (
    build_source_mapping_research_handoff,
)
from app.generation_v2.source_triage_source_analysis_state import has_delivered_source_read
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
    SourceAnalysis,
    SourceFinding,
    SourceCard,
    SourceMap,
    SourceTriageItem,
)
from app.generation_v2.tools.evidence_source_map_ids import source_map_evidence_ids
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id


FINAL_OUTCOME_STATUSES = {"covered", "partial", "missing", "conflicted", "deferred"}
FINAL_SET_STATUSES = {"mapped", "deferred"}
REPRESENTATION_DEFER_BLOCKERS = {"partial", "not_target_content", "unreadable"}
TARGET_CONTENT_ABSENT_STATUSES = {"not_target_content", "unreadable"}
LEGACY_FINALIZE_SOURCE_MAP_GAP = "Triage did not call finalize_source_map."

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
    linked_findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict)
        and any(
            isinstance(link, dict) and str(link.get("question_id") or "") == question_id
            for link in item.get("question_links") or []
        )
    ]
    linked_hints = [
        item
        for item in workspace.get("coverage_hints") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
    ]
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
    finding_refs = {
        source_ref_by_id.get(str(item.get("source_id") or ""), "")
        for item in linked_findings
    }
    hint_refs = {str(item.get("source_ref") or "") for item in linked_hints}
    allowed_ref_set = {ref for ref in linked_assessment_refs | outcome_refs | finding_refs | hint_refs if ref}
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
            *[
                str(evidence_id)
                for finding in linked_findings
                for evidence_id in finding.get("evidence_ids") or []
            ],
            *[
                str(evidence_id)
                for hint in linked_hints
                for evidence_id in hint.get("candidate_evidence_ids") or []
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
    linked_findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict)
        and any(
            isinstance(link, dict) and str(link.get("question_id") or "") == question_id
            for link in item.get("question_links") or []
        )
    ]
    linked_hints = [
        item
        for item in workspace.get("coverage_hints") or []
        if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
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
            (_coverage_input_source_set(item) for item in assigned_sets),
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
        "source_findings": sorted(
            (_coverage_input_source_finding(item) for item in linked_findings),
            key=lambda item: str(item.get("finding_id") or ""),
        ),
        "coverage_hints": sorted(
            (_without_volatile_fields(item) for item in linked_hints),
            key=lambda item: str(item.get("hint_id") or ""),
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _without_volatile_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in {"created_at", "updated_at", "started_at"}}


def _coverage_input_source_set(item: dict[str, Any]) -> dict[str, Any]:
    """Exclude derived-map bookkeeping from synthesis freshness inputs."""

    return {
        key: value
        for key, value in _without_volatile_fields(item).items()
        if key not in {"written_actions", "evidence_ids"}
    }


def _coverage_input_source_finding(item: dict[str, Any]) -> dict[str, Any]:
    """Source relations do not change what a finding says or which question it supports."""

    return {
        key: value
        for key, value in _without_volatile_fields(item).items()
        if key != "source_relations"
    }


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
    questions_by_id = {
        str(item.get("question_id") or ""): item
        for item in questions
        if str(item.get("question_id") or "")
    }
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
    hierarchy_cycle = _question_parent_cycle(questions)
    if hierarchy_cycle:
        violations.append(
            {
                "code": "cyclic_coverage_question_hierarchy",
                "message": "Coverage question parent links cannot contain a cycle.",
                "question_ids": hierarchy_cycle,
            }
        )
    leaf_question_ids = known_question_ids - {
        str(item.get("parent_question_id") or "")
        for item in questions
        if str(item.get("parent_question_id") or "").strip()
    }
    if not leaf_question_ids:
        leaf_question_ids = set(known_question_ids)
    central_question_id = str(workspace.get("central_question_id") or "").strip()
    if central_question_id:
        central_question = next(
            (item for item in questions if str(item.get("question_id") or "") == central_question_id),
            None,
        )
        if (
            central_question is None
            or str(central_question.get("parent_question_id") or "").strip()
            or str(central_question.get("importance") or "") != "critical"
        ):
            violations.append(
                {
                    "code": "invalid_central_coverage_question",
                    "message": "The declared central question must be a persisted critical root question.",
                    "question_ids": [central_question_id],
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
    invalid_set_kind_ids: list[str] = []
    coverage_sets_without_questions: list[str] = []
    orientation_sets_with_planned_questions: list[str] = []
    duplicate_question_assignment_pairs: list[list[str]] = []
    for source_set in source_sets:
        set_id = str(source_set.get("set_id") or "")
        set_kind = str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE)
        planned_question_ids = [
            str(item)
            for item in source_set.get("planned_question_ids", source_set.get("question_ids") or [])
        ]
        if set_kind not in {SOURCE_SET_KIND_COVERAGE, SOURCE_SET_KIND_ORIENTATION}:
            invalid_set_kind_ids.append(set_id)
        elif set_kind == SOURCE_SET_KIND_COVERAGE:
            if not planned_question_ids and not source_set.get("repair"):
                coverage_sets_without_questions.append(set_id)
            assigned_question_ids.update(planned_question_ids)
        elif planned_question_ids:
            orientation_sets_with_planned_questions.append(set_id)
        assignment_question_ids = [
            str(item.get("question_id") or "")
            for item in source_set.get("question_assignments") or []
            if isinstance(item, dict)
        ]
        duplicate_question_assignment_pairs.extend(
            [set_id, question_id]
            for question_id in _duplicates(assignment_question_ids)
        )
        for source_ref in source_set.get("source_refs") or []:
            set_ref_counts[str(source_ref)] += 1
        for question_id in source_set.get("question_ids") or []:
            question_id = str(question_id)
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
    if invalid_set_kind_ids:
        violations.append(
            {
                "code": "invalid_source_set_kind",
                "message": "A source set has an unknown structural kind.",
                "set_ids": sorted(invalid_set_kind_ids),
            }
        )
    if coverage_sets_without_questions:
        violations.append(
            {
                "code": "coverage_source_set_without_questions",
                "message": "A coverage source set needs at least one planner-assigned coverage question.",
                "set_ids": sorted(coverage_sets_without_questions),
            }
        )
    if orientation_sets_with_planned_questions:
        violations.append(
            {
                "code": "orientation_source_set_has_planned_questions",
                "message": "An initial-reading set cannot contain planner-assigned coverage questions.",
                "set_ids": sorted(orientation_sets_with_planned_questions),
            }
        )
    if duplicate_question_assignment_pairs:
        violations.append(
            {
                "code": "duplicate_source_set_question_assignments",
                "message": "A source set may contain only one assignment per coverage question.",
                "set_question_pairs": sorted(duplicate_question_assignment_pairs),
            }
        )
    inspected_refs = {str(item) for item in workspace.get("planner_inspected_source_refs") or []}
    uninspected_orientation_refs = sorted(
        {
            str(source_ref)
            for source_set in source_sets
            if str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE) == SOURCE_SET_KIND_ORIENTATION
            for source_ref in source_set.get("source_refs") or []
            if str(source_ref) not in inspected_refs
        }
    )
    if uninspected_orientation_refs:
        violations.append(
            {
                "code": "uninspected_orientation_sources",
                "message": "Initial-reading routing requires an explicit planner capsule inspection.",
                "source_refs": uninspected_orientation_refs,
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
    unassigned_question_ids = sorted(leaf_question_ids - assigned_question_ids)
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
            str(assessment.get("representation_status") or "unchecked")
            in TARGET_CONTENT_ABSENT_STATUSES
            and str(assessment.get("disposition") or "") != "deferred"
        )
        or (
            str(assessment.get("disposition") or "") == "deferred"
            and str(assessment.get("representation_status") or "unchecked")
            not in REPRESENTATION_DEFER_BLOCKERS
        )
        or (
            str(assessment.get("representation_status") or "unchecked")
            in TARGET_CONTENT_ABSENT_STATUSES
            and (
                str(assessment.get("processing_status") or "") != "resolved"
                or str(assessment.get("relevance") or "unknown") != "unknown"
                or str(assessment.get("authority") or "unknown") != "unknown"
                or str(assessment.get("uniqueness") or "unknown") != "unknown"
                or bool(str(assessment.get("relevance_reason") or "").strip())
                or bool(str(assessment.get("authority_reason") or "").strip())
                or bool(str(assessment.get("uniqueness_reason") or "").strip())
                or bool(assessment.get("coverage_question_ids"))
                or bool(assessment.get("editorial_functions"))
                or (
                    str(assessment.get("representation_status") or "") == "not_target_content"
                    and not bool(assessment.get("evidence_ids"))
                    and not has_delivered_source_read(
                        workspace,
                        set_id=str(assessment.get("set_id") or ""),
                        source_ref=source_ref,
                    )
                )
            )
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
                    "Covered, partial, or conflicted outcomes cannot use not-target-content, unreadable, "
                    "out-of-scope, or unusable sources."
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
    source_set_by_id = {
        str(item.get("set_id") or ""): item
        for item in source_sets
        if str(item.get("set_id") or "")
    }
    hint_violations = _coverage_hint_violations(
        workspace,
        source_set_by_id=source_set_by_id,
        questions_by_id=questions_by_id,
        current_assessment_by_ref=current_assessment_by_ref,
    )
    violations.extend(hint_violations)

    incomplete_outcomes: list[list[str]] = []
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
                in REPRESENTATION_DEFER_BLOCKERS
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


def _coverage_hint_violations(
    workspace: dict[str, Any],
    *,
    source_set_by_id: dict[str, dict[str, Any]],
    questions_by_id: dict[str, dict[str, Any]],
    current_assessment_by_ref: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate coverage hints as mechanical pointers, not as semantic answers."""

    hints = [item for item in workspace.get("coverage_hints") or [] if isinstance(item, dict)]
    if not hints:
        return []

    finding_by_id = {
        str(item.get("finding_id") or ""): item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("finding_id") or "")
    }
    evidence_ids_by_set_source: dict[tuple[str, str], set[str]] = defaultdict(set)
    for assessment in workspace.get("source_assessments") or []:
        if not isinstance(assessment, dict):
            continue
        key = (str(assessment.get("set_id") or ""), str(assessment.get("source_ref") or ""))
        evidence_ids_by_set_source[key].update(
            str(item) for item in assessment.get("evidence_ids") or [] if str(item).strip()
        )
    for finding in workspace.get("source_findings") or []:
        if not isinstance(finding, dict):
            continue
        key = (str(finding.get("set_id") or ""), str(finding.get("source_ref") or ""))
        evidence_ids_by_set_source[key].update(
            str(item) for item in finding.get("evidence_ids") or [] if str(item).strip()
        )

    duplicate_hint_ids = _duplicates(
        [str(item.get("hint_id") or "") for item in hints if str(item.get("hint_id") or "")]
    )
    invalid_hint_ids: set[str] = set()
    invalid_source_refs: set[str] = set()
    invalid_question_ids: set[str] = set()
    invalid_requirement_ids: set[str] = set()
    invalid_finding_ids: set[str] = set()
    invalid_evidence_ids: set[str] = set()

    for hint in hints:
        hint_id = str(hint.get("hint_id") or "")
        set_id = str(hint.get("set_id") or "")
        source_ref = str(hint.get("source_ref") or "")
        question_id = str(hint.get("question_id") or "")
        source_set = source_set_by_id.get(set_id)
        if not hint_id:
            invalid_hint_ids.add("<missing>")
        if source_set is None or source_ref not in {str(item) for item in source_set.get("source_refs") or []}:
            invalid_hint_ids.add(hint_id or "<missing>")
            invalid_source_refs.add(source_ref or "<missing>")
        if (
            source_ref
            and source_ref in current_assessment_by_ref
            and str((current_assessment_by_ref.get(source_ref) or {}).get("set_id") or "") != set_id
        ):
            invalid_hint_ids.add(hint_id or "<missing>")
            invalid_source_refs.add(source_ref)

        question = questions_by_id.get(question_id)
        if question is None:
            invalid_hint_ids.add(hint_id or "<missing>")
            invalid_question_ids.add(question_id or "<missing>")
            known_requirement_ids: set[str] = set()
        else:
            known_requirement_ids = {
                str(item.get("requirement_id") or "")
                for item in question.get("answer_requirements") or []
                if isinstance(item, dict) and str(item.get("requirement_id") or "")
            }
        for requirement_id in hint.get("requirement_ids") or []:
            requirement_id = str(requirement_id)
            if requirement_id and requirement_id not in known_requirement_ids:
                invalid_hint_ids.add(hint_id or "<missing>")
                invalid_requirement_ids.add(requirement_id)

        key = (set_id, source_ref)
        for finding_id in hint.get("candidate_finding_ids") or []:
            finding_id = str(finding_id)
            finding = finding_by_id.get(finding_id)
            if (
                finding is None
                or str(finding.get("set_id") or "") != set_id
                or str(finding.get("source_ref") or "") != source_ref
            ):
                invalid_hint_ids.add(hint_id or "<missing>")
                invalid_finding_ids.add(finding_id or "<missing>")
        for evidence_id in hint.get("candidate_evidence_ids") or []:
            evidence_id = str(evidence_id)
            if evidence_id and evidence_id not in evidence_ids_by_set_source.get(key, set()):
                invalid_hint_ids.add(hint_id or "<missing>")
                invalid_evidence_ids.add(evidence_id)

        has_material_or_gap = bool(
            [item for item in hint.get("candidate_finding_ids") or [] if str(item).strip()]
            or [item for item in hint.get("candidate_evidence_ids") or [] if str(item).strip()]
            or str(hint.get("observed_gap") or "").strip()
            or str(hint.get("conflict_signal") or "").strip()
            or [item for item in hint.get("notes") or [] if str(item).strip()]
        )
        if not has_material_or_gap:
            invalid_hint_ids.add(hint_id or "<missing>")

    if duplicate_hint_ids:
        invalid_hint_ids.update(duplicate_hint_ids)
    if not invalid_hint_ids:
        return []
    return [
        {
            "code": "invalid_coverage_hints",
            "message": "Coverage hints must point only to current set/source/question/finding/evidence records.",
            "hint_ids": sorted(invalid_hint_ids),
            "source_refs": sorted(ref for ref in invalid_source_refs if ref),
            "question_ids": sorted(question_id for question_id in invalid_question_ids if question_id),
            "requirement_ids": sorted(requirement_id for requirement_id in invalid_requirement_ids if requirement_id),
            "finding_ids": sorted(finding_id for finding_id in invalid_finding_ids if finding_id),
            "evidence_ids": sorted(evidence_id for evidence_id in invalid_evidence_ids if evidence_id),
        }
    ]


def _coverage_synthesis_violations(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate only concrete structure of the run-wide question synthesis."""

    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    syntheses = [item for item in workspace.get("coverage_syntheses") or [] if isinstance(item, dict)]
    outcomes = [item for item in workspace.get("coverage_outcomes") or [] if isinstance(item, dict)]
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
    set_id_by_source_ref = {
        str(source_ref): str(source_set.get("set_id") or "")
        for source_set in workspace.get("source_sets") or []
        if isinstance(source_set, dict)
        for source_ref in source_set.get("source_refs") or []
    }
    source_ref_by_id = {source_id: source_ref for source_ref, source_id in source_id_by_ref.items()}
    known_source_refs = {str(item) for item in workspace.get("source_order") or []}
    persisted_evidence_refs: dict[str, set[str]] = defaultdict(set)
    for item in workspace.get("source_assessments") or []:
        if not isinstance(item, dict):
            continue
        source_ref = str(item.get("source_ref") or "")
        for evidence_id in item.get("evidence_ids") or []:
            if str(evidence_id).strip() and source_ref:
                persisted_evidence_refs[str(evidence_id)].add(source_ref)
    for item in workspace.get("source_findings") or []:
        if not isinstance(item, dict):
            continue
        source_ref = source_ref_by_id.get(str(item.get("source_id") or ""), "")
        for evidence_id in item.get("evidence_ids") or []:
            if str(evidence_id).strip() and source_ref:
                persisted_evidence_refs[str(evidence_id)].add(source_ref)
        for relation in item.get("source_relations") or []:
            if not isinstance(relation, dict):
                continue
            related_ref = source_ref_by_id.get(str(relation.get("related_source_id") or ""), "")
            for evidence_id in relation.get("evidence_ids") or []:
                if not str(evidence_id).strip():
                    continue
                if source_ref:
                    persisted_evidence_refs[str(evidence_id)].add(source_ref)
                if related_ref:
                    persisted_evidence_refs[str(evidence_id)].add(related_ref)
    for item in outcomes:
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
    for hint in workspace.get("coverage_hints") or []:
        if not isinstance(hint, dict):
            continue
        source_ref = str(hint.get("source_ref") or "")
        for evidence_id in hint.get("candidate_evidence_ids") or []:
            evidence_id = str(evidence_id)
            if evidence_id.strip() and source_ref:
                persisted_evidence_refs[evidence_id].add(source_ref)
    persisted_evidence_ids = set(persisted_evidence_refs)
    invalid_syntheses: list[str] = []
    invalid_source_refs: set[str] = set()
    invalid_evidence_ids: set[str] = set()
    current_input_revision = int(workspace.get("triage_semantic_revision") or 0)
    deferred_question_ids: set[str] = set()
    for assessment in assessments_by_ref.values():
        if str(assessment.get("representation_status") or "") not in REPRESENTATION_DEFER_BLOCKERS:
            continue
        source_ref = str(assessment.get("source_ref") or "")
        for question_id in assessment.get("coverage_question_ids") or []:
            if str(question_id):
                deferred_question_ids.add(str(question_id))
        for hint in workspace.get("coverage_hints") or []:
            if (
                isinstance(hint, dict)
                and str(hint.get("source_ref") or "") == source_ref
                and str(hint.get("question_id") or "")
            ):
                deferred_question_ids.add(str(hint.get("question_id") or ""))
        for outcome in outcomes:
            if (
                isinstance(outcome, dict)
                and str(outcome.get("status") or "") == "deferred"
                and str(outcome.get("set_id") or "")
                == str(set_id_by_source_ref.get(source_ref) or "")
                and str(outcome.get("question_id") or "")
            ):
                deferred_question_ids.add(str(outcome.get("question_id") or ""))
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
                and question_id in deferred_question_ids
            )
        if str(item.get("next_action") or "") not in {"research", "source_acquisition", "omit", "none"}:
            valid = False
        if status == "covered" and str(item.get("next_action") or "") == "source_acquisition":
            valid = False
        if int(item.get("input_revision") or 0) != current_input_revision:
            valid = False
        if str(item.get("input_hash") or "") != coverage_synthesis_input_hash(workspace, question_id):
            valid = False
        if source_ids != _dedupe([source_id_by_ref.get(ref, "") for ref in source_refs]):
            valid = False
        material_lineage = coverage_question_material_lineage(workspace, question_id)
        question_outcomes = [
            outcome
            for outcome in workspace.get("coverage_outcomes") or []
            if isinstance(outcome, dict) and str(outcome.get("question_id") or "") == question_id
        ]
        active_outcomes = [
            outcome
            for outcome in question_outcomes
            if str(outcome.get("status") or "") in {"covered", "partial", "conflicted"}
        ]
        active_outcome_refs = {
            *[
                str(source_ref)
                for outcome in active_outcomes
                for source_ref in outcome.get("source_refs") or []
            ],
            *[
                source_ref_by_id.get(str(source_id), "")
                for outcome in active_outcomes
                for source_id in outcome.get("source_ids") or []
            ],
        } - {""}
        active_outcome_evidence_ids = {
            str(evidence_id)
            for outcome in active_outcomes
            for evidence_id in outcome.get("evidence_ids") or []
            if str(evidence_id).strip()
        }
        unknown_refs = set(source_refs) - known_source_refs
        allowed_refs = set(material_lineage["source_refs"])
        if status in {"covered", "partial", "conflicted"} and active_outcome_refs:
            allowed_refs.update(active_outcome_refs)
        unlinked_refs = set(source_refs) - allowed_refs
        ineligible_refs = {
            ref
            for ref in source_refs
            if status in {"covered", "partial", "conflicted"}
            and (ref not in assessments_by_ref or not research_eligible(assessments_by_ref[ref]))
        }
        stale_evidence = set(evidence_ids) - persisted_evidence_ids
        allowed_evidence = set(material_lineage["evidence_ids"])
        if status in {"covered", "partial", "conflicted"} and active_outcome_evidence_ids:
            allowed_evidence.update(active_outcome_evidence_ids)
        unlinked_evidence = set(evidence_ids) - allowed_evidence
        misbound_evidence = {
            evidence_id
            for evidence_id in evidence_ids
            if evidence_id in persisted_evidence_refs
            and not persisted_evidence_refs[evidence_id].intersection(source_refs)
        }
        unbacked_refs = {
            source_ref
            for source_ref in source_refs
            if status in {"covered", "partial", "conflicted"}
            and not any(source_ref in persisted_evidence_refs.get(evidence_id, set()) for evidence_id in evidence_ids)
        }
        invalid_source_refs.update(unknown_refs | unlinked_refs | ineligible_refs | unbacked_refs)
        invalid_evidence_ids.update(stale_evidence | unlinked_evidence | misbound_evidence)
        if (
            not valid
            or unknown_refs
            or unlinked_refs
            or ineligible_refs
            or unbacked_refs
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


def _canonical_workspace_evidence_ids(workspace: dict[str, Any]) -> list[str]:
    """Evidence promoted by durable final triage records, never raw observations."""

    evidence_ids = [
        str(evidence_id)
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict) and str(item.get("processing_status") or "resolved") == "resolved"
        for evidence_id in item.get("evidence_ids") or []
    ]
    evidence_ids.extend(
        str(evidence_id)
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict)
        for evidence_id in item.get("evidence_ids") or []
    )
    evidence_ids.extend(
        str(evidence_id)
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict)
        for relation in item.get("source_relations") or []
        if isinstance(relation, dict)
        for evidence_id in relation.get("evidence_ids") or []
    )
    evidence_ids.extend(
        str(evidence_id)
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("status") or "") in FINAL_OUTCOME_STATUSES
        for evidence_id in item.get("evidence_ids") or []
    )
    evidence_ids.extend(
        str(evidence_id)
        for item in workspace.get("coverage_hints") or []
        if isinstance(item, dict)
        for evidence_id in item.get("candidate_evidence_ids") or []
    )
    current_revision = int(workspace.get("triage_semantic_revision") or 0)
    for item in workspace.get("coverage_syntheses") or []:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("question_id") or "")
        if (
            not question_id
            or str(item.get("status") or "") not in FINAL_OUTCOME_STATUSES
            or int(item.get("input_revision") or 0) != current_revision
            or str(item.get("input_hash") or "") != coverage_synthesis_input_hash(workspace, question_id)
        ):
            continue
        evidence_ids.extend(str(value) for value in item.get("evidence_ids") or [])
    return _dedupe(evidence_ids)


def canonical_triage_evidence_ids(
    source_map: SourceMap,
    *,
    workspace: dict[str, Any],
) -> list[str]:
    """One evidence selection shared by SourceMap and CorpusBrief materialization."""

    return _dedupe(
        [
            *source_map_evidence_ids(source_map),
            *_canonical_workspace_evidence_ids(workspace),
        ]
    )


def _canonical_source_set_evidence_ids(
    source_set: dict[str, Any],
    *,
    workspace: dict[str, Any],
    canonical_evidence_ids: set[str],
) -> list[str]:
    set_id = str(source_set.get("set_id") or "")
    candidates = [
        str(evidence_id)
        for action in source_set.get("written_actions") or []
        if isinstance(action, dict)
        for evidence_id in action.get("evidence_ids") or []
    ]
    if not is_structured_triage_workspace(workspace):
        candidates.extend(str(value) for value in source_set.get("evidence_ids") or [])
    candidates.extend(
        str(evidence_id)
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "resolved") == "resolved"
        for evidence_id in item.get("evidence_ids") or []
    )
    candidates.extend(
        str(evidence_id)
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        for evidence_id in item.get("evidence_ids") or []
    )
    candidates.extend(
        str(evidence_id)
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        for relation in item.get("source_relations") or []
        if isinstance(relation, dict)
        for evidence_id in relation.get("evidence_ids") or []
    )
    candidates.extend(
        str(evidence_id)
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("status") or "") in FINAL_OUTCOME_STATUSES
        for evidence_id in item.get("evidence_ids") or []
    )
    return [
        evidence_id
        for evidence_id in _dedupe(candidates)
        if evidence_id in canonical_evidence_ids
    ]


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


def _research_requirement_ids(
    question: CoverageQuestion,
    outcome: CoverageOutcome,
) -> list[str]:
    if not outcome.requirement_results:
        return (
            [item.requirement_id for item in question.answer_requirements]
            if outcome_routes_to_research(outcome)
            else []
        )
    routed_ids = set(requirement_ids_routed_to_research(outcome))
    return [
        requirement.requirement_id
        for requirement in question.answer_requirements
        if requirement.requirement_id in routed_ids
    ]


def _handoff_finding_ids(
    outcome: CoverageOutcome,
    *,
    preferred_source_ids: list[str],
    finding_by_id: dict[str, SourceFinding],
) -> list[str]:
    allowed_source_ids = set(preferred_source_ids)
    return [
        finding_id
        for finding_id in _research_finding_ids(outcome)
        if finding_id in finding_by_id
        and finding_by_id[finding_id].source_id in allowed_source_ids
    ]


def _research_finding_ids(outcome: CoverageOutcome) -> list[str]:
    if not outcome.requirement_results:
        return list(outcome.finding_ids)
    return _dedupe(
        [
            finding_id
            for result in outcome.requirement_results
            if str(result.next_action) == "research"
            for finding_id in result.finding_ids
        ]
    )


def _research_source_ids(
    outcome: CoverageOutcome,
    *,
    finding_by_id: dict[str, SourceFinding],
) -> list[str]:
    if not outcome.requirement_results:
        return list(outcome.source_ids)
    return _dedupe(
        [
            finding_by_id[finding_id].source_id
            for finding_id in _research_finding_ids(outcome)
            if finding_id in finding_by_id
        ]
    )


def _research_handoff_evidence_ids(
    outcome: CoverageOutcome,
    *,
    preferred_source_ids: list[str],
    assessment_by_source: dict[str, SourceAssessment],
    evidence_ids_by_source: dict[str, set[str]],
    finding_by_id: dict[str, SourceFinding],
) -> list[str]:
    if not outcome.requirement_results:
        return handoff_evidence_ids(
            outcome,
            preferred_source_ids=preferred_source_ids,
            assessment_by_source=assessment_by_source,
            evidence_ids_by_source=evidence_ids_by_source,
        )
    allowed_outcome_evidence_ids = set(outcome.evidence_ids)
    return _dedupe(
        [
            evidence_id
            for finding_id in _handoff_finding_ids(
                outcome,
                preferred_source_ids=preferred_source_ids,
                finding_by_id=finding_by_id,
            )
            for evidence_id in finding_by_id[finding_id].evidence_ids
            if evidence_id in allowed_outcome_evidence_ids
        ]
    )


def _source_findings_from_workspace(workspace: dict[str, Any]) -> list[SourceFinding]:
    raw_findings = [item for item in workspace.get("source_findings") or [] if isinstance(item, dict)]
    id_counts: dict[str, int] = defaultdict(int)
    for item in raw_findings:
        finding_id = str(item.get("finding_id") or "").strip()
        if finding_id:
            id_counts[finding_id] += 1
    findings: list[SourceFinding] = []
    for index, item in enumerate(raw_findings, 1):
        payload = dict(item)
        raw_finding_id = str(payload.get("finding_id") or "").strip()
        payload["finding_id"] = _canonical_source_mapping_finding_id(
            payload,
            index=index,
            force=not raw_finding_id or id_counts[raw_finding_id] > 1,
        )
        findings.append(SourceFinding.model_validate(payload))
    return findings


def _canonical_source_mapping_finding_id(payload: dict[str, Any], *, index: int, force: bool) -> str:
    finding_id = str(payload.get("finding_id") or "").strip()
    if finding_id and not force:
        return finding_id
    source_ref = str(payload.get("source_ref") or "").strip()
    if not source_ref:
        return finding_id or f"finding:{index}"
    prefix = f"finding:{source_ref}:"
    if finding_id.startswith(prefix):
        return finding_id
    suffix = (
        stable_set_id(finding_id, str(payload.get("statement") or ""), index)
        if finding_id
        else hashlib.sha256(
            json.dumps(
                [
                    str(payload.get("statement") or ""),
                    [str(item) for item in payload.get("evidence_ids") or [] if str(item)],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
    )
    return f"{prefix}{suffix}"


def _outcome_with_finding_lineage(
    outcome: CoverageOutcome,
    *,
    finding_by_id: dict[str, SourceFinding],
) -> CoverageOutcome:
    """Derive source/evidence lineage from finding references for the new contract."""

    finding_ids = _dedupe(
        [
            *outcome.finding_ids,
            *[
                finding_id
                for result in outcome.requirement_results
                for finding_id in result.finding_ids
            ],
        ]
    )
    if not finding_ids:
        # Completed artifacts before durable findings retain their stored lineage.
        return outcome
    findings = [finding_by_id[finding_id] for finding_id in finding_ids if finding_id in finding_by_id]
    return outcome.model_copy(
        update={
            "finding_ids": finding_ids,
            "source_ids": _dedupe([finding.source_id for finding in findings]),
            "evidence_ids": _dedupe(
                [evidence_id for finding in findings for evidence_id in finding.evidence_ids]
            ),
        }
    )


def materialize_source_map_v2(
    source_map: SourceMap,
    *,
    workspace: dict[str, Any],
    inventory: SourceMap,
) -> SourceMap:
    """Make the workspace, not trace frequency, the canonical triage state."""

    findings = _source_findings_from_workspace(workspace)
    source_analyses = [
        SourceAnalysis.model_validate(item)
        for item in workspace.get("source_analyses") or []
        if isinstance(item, dict)
    ]
    finding_ids_by_source: dict[str, list[str]] = defaultdict(list)
    finding_evidence_by_source: dict[str, list[str]] = defaultdict(list)
    for finding in findings:
        finding_ids_by_source[finding.source_id].append(finding.finding_id)
        finding_evidence_by_source[finding.source_id].extend(finding.evidence_ids)
    assessments = [
        SourceAssessment.model_validate(item)
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
    ]
    source_mapping_mode = triage_workflow_mode(workspace) == TRIAGE_WORKFLOW_SOURCE_MAPPING
    assessments = [
        item.model_copy(
            update={
                "finding_ids": _dedupe(
                    finding_ids_by_source.get(item.source_id, [])
                    if source_mapping_mode
                    else [*item.finding_ids, *finding_ids_by_source.get(item.source_id, [])]
                )
            }
        )
        for item in assessments
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
    all_evidence_ids = canonical_triage_evidence_ids(source_map, workspace=workspace)

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
        card.evidence_ids = _dedupe(
            [
                *card.evidence_ids,
                *assessment.evidence_ids,
                *finding_evidence_by_source.get(source_id, []),
            ]
        )
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
        if representation_status in TARGET_CONTENT_ABSENT_STATUSES or disposition == "unusable":
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
    materialized_gaps = list(source_map.gaps)
    if source_mapping_mode:
        materialized_gaps = [
            str(gap)
            for gap in materialized_gaps
            if str(gap) != LEGACY_FINALIZE_SOURCE_MAP_GAP
        ]
        triage.gaps = [
            str(gap)
            for gap in triage.gaps
            if str(gap) != LEGACY_FINALIZE_SOURCE_MAP_GAP
        ]
    questions = [
        CoverageQuestion.model_validate(item)
        for item in workspace.get("coverage_questions") or []
        if isinstance(item, dict)
    ]
    outcomes = aggregate_coverage_outcomes(workspace)
    return source_map.model_copy(
        update={
            "schema_version": "source_map.v2",
            "source_cards": cards,
            "source_triage": triage,
            "source_assessments": assessments,
            "source_analyses": source_analyses,
            "coverage_questions": questions,
            "source_findings": findings,
            "coverage_outcomes": outcomes,
            "evidence_ids": _dedupe(all_evidence_ids),
            "gaps": _dedupe(materialized_gaps),
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
    finding_by_id = {
        item.finding_id: item for item in _source_findings_from_workspace(workspace)
    }
    if (
        is_structured_triage_workspace(workspace)
        and triage_workflow_mode(workspace) == TRIAGE_WORKFLOW_SOURCE_MAPPING
    ):
        return []
    if fresh_syntheses:
        return [
            _outcome_with_finding_lineage(
                CoverageOutcome.model_validate(
                    {
                        **synthesis_by_question[question_id],
                        "semantic_revision": int(
                            synthesis_by_question[question_id].get("semantic_revision")
                            or synthesis_by_question[question_id].get("input_revision")
                            or 0
                        ),
                        "is_stale": False,
                    }
                ),
                finding_by_id=finding_by_id,
            )
            for question_id in question_ids
        ]
    if syntheses:
        raise ValueError("Run-wide coverage syntheses are incomplete or stale; finalization must rebuild them.")

    legacy_outcomes = [item for item in workspace.get("coverage_outcomes") or [] if isinstance(item, dict)]
    # Compatibility for completed workspaces created before run-wide
    # question synthesis existed.
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in legacy_outcomes:
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
        if is_structured_triage_workspace(workspace):
            # New V2 per-set outcomes are local observations and never route
            # the run. Only an explicit run-wide synthesis may request source
            # acquisition or omission.
            next_action = "research"
        else:
            next_actions = {str(item.get("next_action") or "research") for item in values}
            next_action = next(
                (
                    candidate
                    for candidate in ["source_acquisition", "research", "omit", "none"]
                    if candidate in next_actions
                ),
                "research",
            )
        finding_ids = _dedupe(
            [
                str(finding_id)
                for item in values
                for finding_id in item.get("finding_ids") or []
            ]
        )
        requirement_results = _aggregate_requirement_results(values)
        results.append(
            _outcome_with_finding_lineage(
                CoverageOutcome(
                outcome_id=f"outcome:{question_id}",
                question_id=question_id,
                status=status,
                source_ids=_dedupe([str(source_id) for item in values for source_id in item.get("source_ids") or []]),
                evidence_ids=_dedupe(
                    [str(evidence_id) for item in values for evidence_id in item.get("evidence_ids") or []]
                ),
                finding_ids=finding_ids,
                requirement_results=requirement_results,
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
                semantic_revision=max(
                    [int(item.get("semantic_revision") or 0) for item in values] or [0]
                ),
                is_stale=any(bool(item.get("is_stale")) for item in values),
                ),
                finding_by_id=finding_by_id,
            )
        )
    return results


def _aggregate_requirement_results(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for value in values:
        for result in value.get("requirement_results") or []:
            if not isinstance(result, dict):
                continue
            requirement_id = str(result.get("requirement_id") or "")
            if not requirement_id:
                continue
            if requirement_id not in grouped:
                order.append(requirement_id)
            grouped[requirement_id].append(result)
    aggregated: list[dict[str, Any]] = []
    for requirement_id in order:
        parts = grouped[requirement_id]
        statuses = {str(item.get("status") or "unknown") for item in parts}
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
        aggregated.append(
            {
                "requirement_id": requirement_id,
                "status": status,
                "finding_ids": _dedupe(
                    [
                        str(finding_id)
                        for item in parts
                        for finding_id in item.get("finding_ids") or []
                    ]
                ),
                "summary": _join_semantic(parts, "summary"),
                "closure_basis": _join_semantic(parts, "closure_basis"),
                "remaining_uncertainty": _join_semantic(parts, "remaining_uncertainty"),
                "missing_detail": _join_semantic(parts, "missing_detail"),
                "conflict_summary": _join_semantic(parts, "conflict_summary"),
                "deferred_reason": _join_semantic(parts, "deferred_reason"),
                "confidence": min(
                    [
                        float(0.5 if item.get("confidence") is None else item.get("confidence"))
                        for item in parts
                    ]
                    or [0.5]
                ),
                "next_action": next(
                    (
                        str(item.get("next_action") or "research")
                        for item in parts
                        if str(item.get("next_action") or "research") != "none"
                    ),
                    "none",
                ),
            }
        )
    return aggregated


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
    canonical_evidence_ids = set(source_map_evidence_ids(source_map))
    missing_final_evidence_ids = sorted(
        {
            evidence_id
            for outcome in outcomes
            for evidence_id in outcome.evidence_ids
            if evidence_id not in canonical_evidence_ids
        }
    )
    if missing_final_evidence_ids:
        raise ValueError(
            "Final coverage outcomes reference evidence outside the canonical SourceMap selection: "
            + ", ".join(missing_final_evidence_ids)
        )
    assessment_by_source = {item.source_id: item for item in source_map.source_assessments}
    finding_by_id = {item.finding_id: item for item in source_map.source_findings}
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
                set_kind=str(item.get("set_kind") or SOURCE_SET_KIND_COVERAGE),
                label=str(item.get("label") or ""),
                purpose=str(item.get("purpose") or ""),
                rationale=str(item.get("rationale") or ""),
                source_ids=source_ids,
                question_ids=[str(value) for value in item.get("question_ids") or []],
                question_assignments=[
                    value
                    for value in item.get("question_assignments") or []
                    if isinstance(value, dict)
                ],
                priority=str(item.get("priority") or "normal"),
                status=str(item.get("status") or "mapped"),
                findings_summary=str(item.get("findings_summary") or ""),
                evidence_ids=_canonical_source_set_evidence_ids(
                    item,
                    workspace=workspace,
                    canonical_evidence_ids=canonical_evidence_ids,
                ),
                deferred_reason=str(item.get("deferred_reason") or ""),
            )
        )

    if is_structured_triage_workspace(workspace):
        ref_context = build_source_ref_context(source_map)
        source_cards_by_ref = {
            source_ref_for_id(card.source_id, ref_context): card for card in source_map.source_cards
        }
        handoff = build_source_mapping_research_handoff(
            questions=questions,
            workspace=workspace,
            source_cards_by_ref=source_cards_by_ref,
        )
    else:
        question_by_id = {item.question_id: item for item in questions}
        handoff = []
        for outcome in outcomes:
            if str(outcome.status) == "deferred" or not outcome_routes_to_research(outcome):
                continue
            routed_source_ids = _research_source_ids(outcome, finding_by_id=finding_by_id)
            preferred_source_ids = [
                source_id
                for source_id in routed_source_ids
                if source_id in assessment_by_source
                and source_id not in deferred_source_ids
                and research_eligible(assessment_by_source[source_id])
            ]
            if routed_source_ids and not preferred_source_ids:
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
                    question_assignments=(
                        [
                            {
                                "question_id": outcome.question_id,
                                "requirement_ids": _research_requirement_ids(question, outcome),
                            }
                        ]
                        if question is not None and question.answer_requirements
                        else []
                    ),
                    finding_ids=_handoff_finding_ids(
                        outcome,
                        preferred_source_ids=preferred_source_ids,
                        finding_by_id=finding_by_id,
                    ),
                    preferred_source_ids=preferred_source_ids,
                    starting_evidence_ids=_research_handoff_evidence_ids(
                        outcome,
                        preferred_source_ids=preferred_source_ids,
                        assessment_by_source=assessment_by_source,
                        evidence_ids_by_source=evidence_ids_by_source,
                        finding_by_id=finding_by_id,
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
    declared_central_question_id = str(workspace.get("central_question_id") or "")
    central_question = next(
        (item.question for item in questions if item.question_id == declared_central_question_id),
        next(
            (item.question for item in questions if str(item.importance) == "critical"),
            questions[0].question if questions else str(intent_payload.get("user_prompt") or ""),
        ),
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
        schema_version="corpus_brief.v2",
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
        source_findings=[item.model_copy(deep=True) for item in source_map.source_findings],
        source_analyses=[item.model_copy(deep=True) for item in source_map.source_analyses],
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
    parent_question_ids = {item.parent_question_id for item in brief.coverage_questions if item.parent_question_id}
    leaf_question_ids = valid_question_ids - parent_question_ids or valid_question_ids
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
        *(source_id for item in brief.research_handoff for source_id in item.allowed_source_ids),
        *(source_id for item in brief.research_handoff for source_id in item.preferred_source_ids),
        *(source_id for item in brief.research_handoff for source_id in item.candidate_source_ids),
        *(source_id for item in brief.gap_records for source_id in item.source_ids),
        *(finding.source_id for finding in brief.source_findings),
        *(
            relation.related_source_id
            for finding in brief.source_findings
            for relation in finding.source_relations
        ),
    }
    referenced_evidence_ids = {
        *(evidence_id for source_set in brief.source_sets for evidence_id in source_set.evidence_ids),
        *(evidence_id for outcome in brief.coverage_outcomes for evidence_id in outcome.evidence_ids),
        *(evidence_id for item in brief.research_handoff for evidence_id in item.evidence_ids),
        *(evidence_id for item in brief.research_handoff for evidence_id in item.starting_evidence_ids),
        *(
            evidence_id
            for item in brief.research_handoff
            for finding in item.findings
            if isinstance(finding, dict)
            for evidence_id in finding.get("evidence_ids") or []
        ),
        *(evidence_id for item in brief.gap_records for evidence_id in item.evidence_ids),
        *(
            evidence_id
            for finding in brief.source_findings
            for evidence_id in finding.evidence_ids
        ),
        *(
            evidence_id
            for finding in brief.source_findings
            for relation in finding.source_relations
            for evidence_id in relation.evidence_ids
        ),
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
    if source_map.coverage_questions or source_map.source_findings or source_map.coverage_outcomes:
        lineage_matches = (
            [item.model_dump(mode="json") for item in brief.coverage_questions]
            == [item.model_dump(mode="json") for item in source_map.coverage_questions]
            and [item.model_dump(mode="json") for item in brief.source_findings]
            == [item.model_dump(mode="json") for item in source_map.source_findings]
            and [item.model_dump(mode="json") for item in brief.coverage_outcomes]
            == [item.model_dump(mode="json") for item in source_map.coverage_outcomes]
        )
        if not lineage_matches:
            issues.append(
                CorpusBriefAuditIssue(
                    issue_id="objective:finding_lineage",
                    code="unsupported_coverage",
                    severity="blocking",
                    message=(
                        "CorpusBrief must preserve SourceMap questions, findings, requirement results, "
                        "and coverage lineage without changing them."
                    ),
                    suggested_repair="Rebuild CorpusBrief from the current canonical SourceMap.",
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
    source_mapping_handoff = _uses_source_mapping_handoff(brief)
    actual_handoff_question_ids = [question_id for item in brief.research_handoff for question_id in item.question_ids]
    if source_mapping_handoff:
        missing_handoff_question_ids = sorted(leaf_question_ids - set(actual_handoff_question_ids))
        if missing_handoff_question_ids:
            issues.append(
                CorpusBriefAuditIssue(
                    issue_id="objective:unresolved_question",
                    code="unresolved_question",
                    severity="blocking",
                    message="Every coverage question must be handed off to research.",
                    question_ids=missing_handoff_question_ids,
                    suggested_repair="Rebuild research handoff from coverage questions and source mapping.",
                )
            )
    else:
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
            for source_id in (
                []
                if source_mapping_handoff
                else (item.allowed_source_ids or item.preferred_source_ids)
            )
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
    finding_by_id = {item.finding_id: item for item in brief.source_findings}
    invalid_handoff_evidence_ids: set[str] = set()
    for item in brief.research_handoff:
        if source_mapping_handoff:
            allowed_evidence_ids = set(item.evidence_ids)
            referenced = {
                str(evidence_id)
                for finding in item.findings
                if isinstance(finding, dict)
                for evidence_id in finding.get("evidence_ids") or []
                if str(evidence_id)
            }
            invalid_handoff_evidence_ids.update(referenced - allowed_evidence_ids)
            invalid_handoff_evidence_ids.update(set(item.starting_evidence_ids) - allowed_evidence_ids)
            invalid_handoff_evidence_ids.update(set(item.candidate_evidence_ids) - allowed_evidence_ids)
            continue
        else:
            allowed_evidence_ids = {
                evidence_id
                for question_id in item.question_ids
                if (outcome := outcome_by_question.get(question_id)) is not None
                for evidence_id in _research_handoff_evidence_ids(
                    outcome,
                    preferred_source_ids=item.preferred_source_ids,
                    assessment_by_source=assessment_by_source,
                    evidence_ids_by_source=evidence_ids_by_source,
                    finding_by_id=finding_by_id,
                )
            }
        invalid_handoff_evidence_ids.update(set(item.starting_evidence_ids) - allowed_evidence_ids)
    if invalid_handoff_evidence_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:ineligible_evidence_in_handoff",
                code="deferred_source_in_handoff",
                severity="blocking",
                message="Research handoff evidence must belong to eligible source-mapping material.",
                evidence_ids=sorted(invalid_handoff_evidence_ids),
                suggested_repair="Rebuild the handoff from the current SourceMap and source-mapping workspace.",
            )
        )
    if source_mapping_handoff:
        expected_handoff_question_ids = set(leaf_question_ids)
    else:
        expected_handoff_question_ids: set[str] = set()
        for outcome in brief.coverage_outcomes:
            if str(outcome.status) == "deferred" or not outcome_routes_to_research(outcome):
                continue
            routed_source_ids = _research_source_ids(outcome, finding_by_id=finding_by_id)
            if routed_source_ids and not any(
                source_id in assessment_by_source
                and source_id not in deferred_set_source_ids
                and research_eligible(assessment_by_source[source_id])
                for source_id in routed_source_ids
            ):
                continue
            expected_handoff_question_ids.add(outcome.question_id)
    mismatched_handoff_question_ids = sorted(
        expected_handoff_question_ids.symmetric_difference(actual_handoff_question_ids)
        | set(_duplicates(actual_handoff_question_ids))
    )
    for item in brief.research_handoff:
        if source_mapping_handoff:
            for finding in item.findings:
                if not isinstance(finding, dict):
                    mismatched_handoff_question_ids.extend(item.question_ids)
                    continue
                if str(finding.get("source_ref") or "") not in set(item.source_refs):
                    mismatched_handoff_question_ids.extend(item.question_ids)
            continue
        for question_id in item.question_ids:
            outcome = outcome_by_question.get(question_id)
            if outcome is None:
                continue
            expected_preferred_source_ids = [
                source_id
                for source_id in _research_source_ids(outcome, finding_by_id=finding_by_id)
                if source_id in assessment_by_source
                and source_id not in deferred_set_source_ids
                and research_eligible(assessment_by_source[source_id])
            ]
            if item.preferred_source_ids != expected_preferred_source_ids:
                mismatched_handoff_question_ids.append(question_id)
            expected_evidence_ids = _research_handoff_evidence_ids(
                outcome,
                preferred_source_ids=expected_preferred_source_ids,
                assessment_by_source=assessment_by_source,
                evidence_ids_by_source=evidence_ids_by_source,
                finding_by_id=finding_by_id,
            )
            if item.starting_evidence_ids != expected_evidence_ids:
                mismatched_handoff_question_ids.append(question_id)
            expected_finding_ids = set(
                _handoff_finding_ids(
                    outcome,
                    preferred_source_ids=expected_preferred_source_ids,
                    finding_by_id=finding_by_id,
                )
            )
            if set(item.finding_ids) != expected_finding_ids:
                mismatched_handoff_question_ids.append(question_id)
            assignment = next(
                (
                    value
                    for value in item.question_assignments
                    if value.question_id == question_id
                ),
                None,
            )
            question = next(
                (value for value in brief.coverage_questions if value.question_id == question_id),
                None,
            )
            expected_requirement_ids = (
                set(_research_requirement_ids(question, outcome)) if question is not None else set()
            )
            actual_requirement_ids = set(assignment.requirement_ids) if assignment is not None else set()
            if actual_requirement_ids != expected_requirement_ids:
                mismatched_handoff_question_ids.append(question_id)
    mismatched_handoff_question_ids = sorted(set(mismatched_handoff_question_ids))
    if mismatched_handoff_question_ids:
        issues.append(
            CorpusBriefAuditIssue(
                issue_id="objective:research_handoff",
                code="unsupported_coverage",
                severity="blocking",
                message="Research handoff must match the current triage handoff contract.",
                question_ids=mismatched_handoff_question_ids,
                suggested_repair="Regenerate research handoff deterministically from SourceMap and triage workspace.",
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


def _uses_source_mapping_handoff(brief: CorpusBrief) -> bool:
    if not brief.coverage_outcomes:
        return True
    if not brief.research_handoff:
        return False
    return all(
        not item.allowed_source_ids
        and not item.preferred_source_ids
        and not item.candidate_source_ids
        and bool(
            item.question
            or item.requirements
            or item.findings
            or item.gaps
            or item.evidence_ids
            or item.source_refs
            or item.source_cards
        )
        for item in brief.research_handoff
    )


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return sorted(duplicate)


def _question_parent_cycle(questions: list[dict[str, Any]]) -> list[str]:
    parent_by_id = {
        str(item.get("question_id") or ""): str(item.get("parent_question_id") or "")
        for item in questions
        if isinstance(item, dict) and str(item.get("question_id") or "")
    }
    for start in parent_by_id:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current and current in parent_by_id:
            if current in positions:
                return path[positions[current] :] + [current]
            positions[current] = len(path)
            path.append(current)
            current = parent_by_id.get(current, "")
    return []


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
    "canonical_triage_evidence_ids",
    "canonical_workspace_violations",
    "materialize_corpus_brief",
    "materialize_source_map_v2",
    "research_eligible",
]
