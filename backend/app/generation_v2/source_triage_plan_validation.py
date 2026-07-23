from __future__ import annotations

from collections import Counter
from typing import Any

from app.generation_v2.source_triage_workspace_state import (
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
)


def validate_triage_plan(
    workspace: dict[str, Any],
    *,
    central_question_id: str | None = None,
) -> dict[str, Any]:
    """Build the one structural validation report used by product and eval."""

    issues: list[str] = []
    source_order = [str(value) for value in workspace.get("source_order") or []]
    source_sets = [item for item in workspace.get("source_sets") or [] if isinstance(item, dict)]
    questions = [item for item in workspace.get("coverage_questions") or [] if isinstance(item, dict)]
    question_ids = [str(item.get("question_id") or "") for item in questions]
    set_ids = [str(item.get("set_id") or "") for item in source_sets]
    _append_identifier_issues(issues, question_ids, label="coverage question")
    _append_identifier_issues(issues, set_ids, label="source set")

    questions_by_id = {
        str(item.get("question_id") or ""): item
        for item in questions
        if str(item.get("question_id") or "")
    }
    requirement_pairs: set[tuple[str, str]] = set()
    for question_id, question in questions_by_id.items():
        requirements = [
            item for item in question.get("answer_requirements") or [] if isinstance(item, dict)
        ]
        if not requirements:
            issues.append(f"coverage question {question_id} has no answer requirements")
            continue
        requirement_ids = [str(item.get("requirement_id") or "") for item in requirements]
        _append_identifier_issues(
            issues,
            requirement_ids,
            label=f"requirement in question {question_id}",
        )
        requirement_pairs.update(
            (question_id, requirement_id)
            for requirement_id in requirement_ids
            if requirement_id
        )

    hierarchy_cycle = question_parent_cycle(questions)
    if hierarchy_cycle:
        issues.append("coverage question hierarchy contains a cycle")

    central_value = (
        workspace.get("central_question_id")
        if central_question_id is None
        else central_question_id
    )
    resolved_central_id = str(central_value or "").strip()
    central_question = questions_by_id.get(resolved_central_id)
    central_error = ""
    if central_question is None:
        central_error = "missing"
        issues.append("central_question_id does not reference a coverage question")
    elif str(central_question.get("parent_question_id") or "").strip():
        central_error = "not_root"
        issues.append("central_question_id does not reference a root question")
    elif str(central_question.get("importance") or "") != "critical":
        central_error = "not_critical"
        issues.append("central_question_id does not reference a critical question")

    assigned_source_refs = [
        str(source_ref)
        for source_set in source_sets
        for source_ref in source_set.get("source_refs") or []
    ]
    assigned_source_counts = Counter(assigned_source_refs)
    known_source_refs = set(source_order)
    duplicate_source_refs = sorted(
        source_ref for source_ref, count in assigned_source_counts.items() if count > 1
    )
    missing_source_refs = sorted(
        source_ref for source_ref in source_order if assigned_source_counts[source_ref] == 0
    )
    unknown_source_refs = sorted(set(assigned_source_refs) - known_source_refs)
    if duplicate_source_refs or missing_source_refs or unknown_source_refs:
        issues.append("source sets do not assign the pinned corpus exactly once")

    valid_set_kinds = {SOURCE_SET_KIND_COVERAGE, SOURCE_SET_KIND_ORIENTATION}
    invalid_source_set_ids: list[str] = []
    coverage_sets_without_question_ids: list[str] = []
    orientation_source_sets_with_question_ids: list[str] = []
    uninspected_orientation_source_refs: set[str] = set()
    inconsistent_question_assignment_set_ids: list[str] = []
    duplicate_question_assignments: list[dict[str, str]] = []
    unknown_question_ids: set[str] = set()
    referenced_question_ids: set[str] = set()
    assigned_requirement_pairs: set[tuple[str, str]] = set()
    inspected_refs = {str(value) for value in workspace.get("planner_inspected_source_refs") or []}

    for source_set in source_sets:
        set_id = str(source_set.get("set_id") or "") or "<unknown>"
        set_kind = str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE)
        if set_kind not in valid_set_kinds:
            invalid_source_set_ids.append(set_id)
            continue
        if not source_set.get("source_refs"):
            issues.append(f"source set {set_id} has no sources")
        planned_ids = [
            str(value)
            for value in source_set.get(
                "planned_question_ids",
                source_set.get("question_ids") or [],
            )
        ]
        declared_ids = [str(value) for value in source_set.get("question_ids") or []]
        assignments = [
            item
            for item in source_set.get("question_assignments") or []
            if isinstance(item, dict)
        ]
        assignment_ids = [str(item.get("question_id") or "") for item in assignments]
        if set_kind == SOURCE_SET_KIND_ORIENTATION:
            if planned_ids or declared_ids or assignments:
                orientation_source_sets_with_question_ids.append(set_id)
            uninspected_orientation_source_refs.update(
                str(source_ref)
                for source_ref in source_set.get("source_refs") or []
                if str(source_ref) not in inspected_refs
            )
            continue
        if not planned_ids:
            coverage_sets_without_question_ids.append(set_id)
        if set(planned_ids) != set(declared_ids) or set(planned_ids) != set(assignment_ids):
            inconsistent_question_assignment_set_ids.append(set_id)
        duplicate_ids = sorted(
            question_id
            for question_id, count in Counter(assignment_ids).items()
            if question_id and count > 1
        )
        duplicate_question_assignments.extend(
            {"set_id": set_id, "question_id": question_id}
            for question_id in duplicate_ids
        )
        referenced_question_ids.update(question_id for question_id in planned_ids if question_id)
        unknown_question_ids.update(
            set(planned_ids + declared_ids + assignment_ids) - set(questions_by_id)
        )
        for assignment in assignments:
            question_id = str(assignment.get("question_id") or "")
            assigned_requirement_pairs.update(
                (question_id, str(requirement_id))
                for requirement_id in assignment.get("requirement_ids") or []
            )

    unassigned_question_ids = sorted(set(questions_by_id) - referenced_question_ids)
    unassigned_requirements = sorted(requirement_pairs - assigned_requirement_pairs)
    unknown_assigned_requirements = sorted(assigned_requirement_pairs - requirement_pairs)
    if invalid_source_set_ids:
        issues.append("source sets contain an invalid set_kind")
    if coverage_sets_without_question_ids:
        issues.append("coverage source sets have no questions")
    if orientation_source_sets_with_question_ids:
        issues.append("orientation source sets have question assignments")
    if uninspected_orientation_source_refs:
        issues.append("orientation sources were not inspected by the planner")
    if inconsistent_question_assignment_set_ids:
        issues.append("source sets have inconsistent question assignments")
    if duplicate_question_assignments:
        issues.append("source sets assign a question more than once")
    if unknown_question_ids:
        issues.append(
            "source sets reference unknown questions: "
            + ", ".join(sorted(unknown_question_ids))
        )
    if unassigned_question_ids:
        issues.append(
            "coverage questions are unassigned: " + ", ".join(unassigned_question_ids)
        )
    if unassigned_requirements:
        issues.append(
            "coverage requirements are unassigned: "
            + ", ".join(
                f"{question_id}/{requirement_id}"
                for question_id, requirement_id in unassigned_requirements
            )
        )
    if unknown_assigned_requirements:
        issues.append(
            "source sets assign unknown requirements: "
            + ", ".join(
                f"{question_id}/{requirement_id}"
                for question_id, requirement_id in unknown_assigned_requirements
            )
        )

    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "source_set_count": len(source_sets),
        "coverage_question_count": len(questions),
        "central_question_id": resolved_central_id,
        "central_question_error": central_error,
        "question_hierarchy_cycle": hierarchy_cycle,
        "duplicate_source_refs": duplicate_source_refs,
        "missing_source_refs": missing_source_refs,
        "unknown_source_refs": unknown_source_refs,
        "invalid_source_set_ids": sorted(invalid_source_set_ids),
        "unknown_question_ids": sorted(unknown_question_ids),
        "unassigned_question_ids": unassigned_question_ids,
        "unassigned_question_requirements": [
            {"question_id": question_id, "requirement_id": requirement_id}
            for question_id, requirement_id in unassigned_requirements
        ],
        "unknown_assigned_question_requirements": [
            {"question_id": question_id, "requirement_id": requirement_id}
            for question_id, requirement_id in unknown_assigned_requirements
        ],
        "coverage_source_sets_without_question_ids": sorted(
            coverage_sets_without_question_ids
        ),
        "orientation_source_sets_with_question_ids": sorted(
            orientation_source_sets_with_question_ids
        ),
        "uninspected_orientation_source_refs": sorted(uninspected_orientation_source_refs),
        "inconsistent_question_assignment_set_ids": sorted(
            inconsistent_question_assignment_set_ids
        ),
        "duplicate_question_assignments": duplicate_question_assignments,
    }


def triage_plan_finalization_error(report: dict[str, Any]) -> dict[str, Any] | None:
    if report.get("valid") is True:
        return None
    if not int(report.get("source_set_count") or 0):
        return {"accepted": False, "error": "Cannot finalize triage workspace without source sets"}
    if report.get("duplicate_source_refs"):
        return {
            "accepted": False,
            "error": "Every source_ref must belong to exactly one source set",
            "duplicate_source_refs": report["duplicate_source_refs"],
        }
    if report.get("missing_source_refs"):
        return {
            "accepted": False,
            "error": "Every source_ref must be assigned to exactly one source set",
            "missing_source_refs": report["missing_source_refs"],
        }
    if not int(report.get("coverage_question_count") or 0):
        return {"accepted": False, "error": "Persist at least one coverage question before finalizing"}
    if report.get("question_hierarchy_cycle"):
        return {
            "accepted": False,
            "error": "coverage question hierarchy cannot contain a cycle",
            "question_ids": report["question_hierarchy_cycle"],
        }
    central_error = str(report.get("central_question_error") or "")
    if central_error:
        message = {
            "missing": "central_question_id must reference a persisted coverage question",
            "not_root": "central_question_id must reference a root question",
            "not_critical": "central_question_id must reference a critical question",
        }[central_error]
        return {
            "accepted": False,
            "error": message,
            "central_question_id": report.get("central_question_id"),
        }
    detail_fields = (
        "invalid_source_set_ids",
        "unknown_question_ids",
        "unassigned_question_ids",
        "unassigned_question_requirements",
        "unknown_assigned_question_requirements",
        "unknown_source_refs",
        "coverage_source_sets_without_question_ids",
        "orientation_source_sets_with_question_ids",
        "uninspected_orientation_source_refs",
        "inconsistent_question_assignment_set_ids",
        "duplicate_question_assignments",
    )
    return {
        "accepted": False,
        "error": "The source-set plan has invalid question or initial-reading assignments",
        **{field: report.get(field) or [] for field in detail_fields},
        "structural_issues": list(report.get("issues") or []),
    }


def triage_plan_violations(workspace: dict[str, Any]) -> list[str]:
    return list(validate_triage_plan(workspace).get("issues") or [])


def question_parent_cycle(questions: list[dict[str, Any]]) -> list[str]:
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


def _append_identifier_issues(issues: list[str], values: list[str], *, label: str) -> None:
    if any(not value for value in values):
        issues.append(f"{label} is missing an id")
    duplicates = sorted(
        value for value, count in Counter(values).items() if value and count > 1
    )
    if duplicates:
        issues.append(f"duplicate {label} ids: {', '.join(duplicates)}")


__all__ = [
    "question_parent_cycle",
    "triage_plan_finalization_error",
    "triage_plan_violations",
    "validate_triage_plan",
]
